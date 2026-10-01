# -*- coding: utf-8 -*-
"""Точки входа для Android-приложения (Chaquopy).

Kotlin вызывает:
  start(files_dir)     — запустить локальный сервер интерфейса, вернуть порт;
  run_once(files_dir)  — фоновый поиск из WorkManager (блокирует до окончания), вернуть сводку JSON;
  schedule(files_dir)  — настройки расписания JSON {enabled, every_hours}.
Python вызывает Kotlin через ru.tendermonitor.Bridge: служба переднего плана, уведомления, расписание.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

_lock = threading.Lock()
_app = None
_srv = None
_port = 0

ANDROID_DEFAULTS = {
    "market": {"max_pages": 2},
    "docs": {"max_tenders_per_run": 30, "max_file_mb": 15},
    "schedule": {"enabled": True, "every_hours": 3, "from_hour": 0, "to_hour": 24},
    "ui": {"open_browser": False},
}


def _bridge():
    try:
        from java import jclass  # type: ignore
        return jclass("ru.tendermonitor.Bridge")
    except Exception:  # noqa: BLE001  (не Android — тесты)
        return None


def _setup_logging(data: Path) -> None:
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(data / "app.log", mode="a", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.addHandler(fh)
    for noisy in ("urllib3", "charset_normalizer", "pypdf"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def _get_app(files_dir: str):
    global _app
    with _lock:
        if _app is not None:
            return _app
        from .app import App
        data = Path(files_dir) / "data"
        data.mkdir(parents=True, exist_ok=True)
        _setup_logging(data)
        app = App(data)
        app.platform = "android"
        if not app.store.kv("android_defaults"):
            s = app.store.settings()
            for k, v in ANDROID_DEFAULTS.items():
                s.setdefault(k, {}).update(v)
            app.store.save_settings(s)
            app.store.set_kv("android_defaults", True)
        br = _bridge()
        if br is not None:
            app.engine.on_start = lambda: br.searchStarted()
            app.engine.on_finish = lambda: br.searchFinished()

            def notify(rows, clf):
                dirs = {}
                for r in rows:
                    for k in json.loads(r["directions"] or "{}"):
                        dirs[clf.name(k)] = dirs.get(clf.name(k), 0) + 1
                title = f"Новые закупки: {len(rows)}"
                summary = ", ".join(f"{k} {v}" for k, v in dirs.items())
                lines = [f"• {(r['title'] or '')[:90]}" for r in rows[:5]]
                br.notifyNew(title, summary, "\n".join(lines), len(rows))
            app.engine.notify_hook = notify

            def resched(s):
                sc = s.get("schedule") or {}
                br.reschedule(bool(sc.get("enabled")), int(float(sc.get("every_hours", 3)) or 3))
            app.on_settings_change = resched
        _app = app
        return app


def start(files_dir: str) -> int:
    """Запускает сервер интерфейса на 127.0.0.1 и возвращает порт."""
    global _srv, _port
    app = _get_app(files_dir)
    with _lock:
        if _srv is not None:
            return _port
        from .app import make_handler
        for p in range(8765, 8790):
            try:
                _srv = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app, p))
                _port = p
                break
            except OSError:
                continue
        if _srv is None:
            raise RuntimeError("Не удалось занять порт для интерфейса")
        threading.Thread(target=_srv.serve_forever, daemon=True).start()
        return _port


def run_once(files_dir: str) -> str:
    """Фоновый поиск по расписанию. Если поиск уже идёт (запущен из интерфейса) — ждём его."""
    app = _get_app(files_dir)
    if not app.engine.start():
        pass
    t0 = time.time()
    while app.engine.progress.running and time.time() - t0 < 3 * 3600:
        time.sleep(3)
    return json.dumps(app.engine.progress.summary or {}, ensure_ascii=False)


def schedule(files_dir: str) -> str:
    app = _get_app(files_dir)
    sc = app.store.settings().get("schedule") or {}
    return json.dumps({"enabled": bool(sc.get("enabled")), "every_hours": int(float(sc.get("every_hours", 3)) or 3)})
