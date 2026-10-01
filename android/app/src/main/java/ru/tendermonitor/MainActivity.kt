package ru.tendermonitor

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.Gravity
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import org.json.JSONObject
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var splash: TextView
    private var port = 0

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = FrameLayout(this)
        web = WebView(this)
        splash = TextView(this).apply {
            text = "Запуск…"
            textSize = 18f
            gravity = Gravity.CENTER
            setTextColor(Color.parseColor("#2F5EA8"))
            setBackgroundColor(Color.WHITE)
        }
        root.addView(web)
        root.addView(splash)
        setContentView(root)

        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.setSupportMultipleWindows(false)
        web.webChromeClient = WebChromeClient()            // confirm()/prompt() интерфейса
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                val u = req.url
                if (u.host == "127.0.0.1" || u.host == "localhost") return false
                // ссылки на площадки и документы открываем во внешнем браузере
                try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: Exception) {}
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                splash.visibility = android.view.View.GONE
            }
        }
        web.setDownloadListener { url, ua, cd, mime, _ ->
            if (Uri.parse(url).host == "127.0.0.1") {           // выгрузка Excel из самого приложения
                saveLocal(url, URLUtil.guessFileName(url, cd, mime), mime)
                return@setDownloadListener
            }
            try {
                val name = URLUtil.guessFileName(url, cd, mime)
                val r = DownloadManager.Request(Uri.parse(url))
                    .setMimeType(mime)
                    .addRequestHeader("User-Agent", ua)
                    .setTitle(name)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(r)
                Toast.makeText(this, "Скачивается в «Загрузки»: $name", Toast.LENGTH_LONG).show()
            } catch (e: Exception) {
                Toast.makeText(this, "Не удалось скачать: ${e.message}", Toast.LENGTH_LONG).show()
            }
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        thread {
            try {
                val mod = Python.getInstance().getModule("tender_app.android_entry")
                val dir = filesDir.absolutePath
                port = mod.callAttr("start", dir).toInt()
                val sc = JSONObject(mod.callAttr("schedule", dir).toString())
                Scheduler.apply(this, sc.optBoolean("enabled", true), sc.optInt("every_hours", 3))
                runOnUiThread { web.loadUrl("http://127.0.0.1:$port/") }
            } catch (e: Exception) {
                runOnUiThread { splash.text = "Ошибка запуска:\n${e.message}" }
            }
        }
    }

    private fun saveLocal(url: String, nameGuess: String, mime: String?) {
        thread {
            try {
                val conn = java.net.URL(url).openConnection() as java.net.HttpURLConnection
                val cd = conn.getHeaderField("Content-Disposition") ?: ""
                val name = Regex("filename=\"?([^\";]+)").find(cd)?.groupValues?.get(1) ?: nameGuess
                val bytes = conn.inputStream.use { it.readBytes() }
                val type = mime ?: "application/octet-stream"
                if (Build.VERSION.SDK_INT >= 29) {
                    val values = android.content.ContentValues().apply {
                        put(android.provider.MediaStore.Downloads.DISPLAY_NAME, name)
                        put(android.provider.MediaStore.Downloads.MIME_TYPE, type)
                    }
                    val uri = contentResolver.insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw Exception("нет доступа к «Загрузкам»")
                    contentResolver.openOutputStream(uri)!!.use { it.write(bytes) }
                } else {
                    val f = java.io.File(getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), name)
                    f.writeBytes(bytes)
                }
                runOnUiThread { Toast.makeText(this, "Сохранено в «Загрузки»: $name", Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Не удалось сохранить: ${e.message}", Toast.LENGTH_LONG).show() }
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }
}
