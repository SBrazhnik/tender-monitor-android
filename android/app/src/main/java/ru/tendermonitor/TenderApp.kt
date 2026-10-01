package ru.tendermonitor

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class TenderApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Bridge.ctx = applicationContext
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CH_NEW, "Новые закупки", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Новые закупки ИТ, ИБ и АСУТП"
            })
        nm.createNotificationChannel(
            NotificationChannel(CH_WORK, "Поиск закупок", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Идёт поиск по площадкам"
            })
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
    }

    companion object {
        const val CH_NEW = "new_tenders"
        const val CH_WORK = "search_work"
    }
}
