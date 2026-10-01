package ru.tendermonitor

import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Фоновый поиск по расписанию. */
class SearchWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun getForegroundInfo(): ForegroundInfo {
        val n = KeepAliveService.workNotification(applicationContext)
        return if (Build.VERSION.SDK_INT >= 29)
            ForegroundInfo(1003, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        else ForegroundInfo(1003, n)
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        try {
            try { setForeground(getForegroundInfo()) } catch (_: Exception) {}
            if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
            Python.getInstance().getModule("tender_app.android_entry")
                .callAttr("run_once", applicationContext.filesDir.absolutePath)
            Result.success()
        } catch (e: Exception) {
            Result.retry()
        }
    }
}
