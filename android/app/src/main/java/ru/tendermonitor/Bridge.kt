package ru.tendermonitor

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/** Методы, которые вызывает Python-движок (android_entry.py). */
@SuppressLint("StaticFieldLeak")
object Bridge {
    lateinit var ctx: Context

    @JvmStatic
    fun searchStarted() {
        val i = Intent(ctx, KeepAliveService::class.java)
        try {
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        } catch (ignored: Exception) {
            // из фона Android может не разрешить — тогда поиск просто идёт, пока жив процесс
        }
    }

    @JvmStatic
    fun searchFinished() {
        try { ctx.stopService(Intent(ctx, KeepAliveService::class.java)) } catch (ignored: Exception) {}
    }

    @JvmStatic
    fun notifyNew(title: String, summary: String, lines: String, count: Int) {
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(ctx, TenderApp.CH_NEW)
            .setSmallIcon(R.drawable.ic_stat_logo)
            .setColor(0xFFFF6A13.toInt())
            .setContentTitle(title)
            .setContentText(summary)
            .setStyle(NotificationCompat.BigTextStyle().bigText(summary + "\n" + lines))
            .setNumber(count)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(1001, n)
        } catch (ignored: SecurityException) {
            // нет разрешения на уведомления
        }
    }

    @JvmStatic
    fun reschedule(enabled: Boolean, hours: Int) {
        Scheduler.apply(ctx, enabled, hours)
    }
}
