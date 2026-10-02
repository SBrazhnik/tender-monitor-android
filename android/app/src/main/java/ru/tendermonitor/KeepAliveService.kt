package ru.tendermonitor

import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/** Служба переднего плана на время поиска, запущенного из интерфейса, чтобы Android не прервал его. */
class KeepAliveService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val n = workNotification(this)
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1002, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(1002, n)
        }
        return START_NOT_STICKY
    }

    companion object {
        fun workNotification(s: android.content.Context) =
            NotificationCompat.Builder(s, TenderApp.CH_WORK)
                .setSmallIcon(R.drawable.ic_stat_logo)
                .setColor(0xFFF8961D.toInt())
                .setContentTitle("Идёт поиск закупок")
                .setContentText("ЕИС, Сбербанк-АСТ, B2B-Center, Росатом")
                .setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(
                    s, 0, android.content.Intent(s, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE))
                .build()
    }
}
