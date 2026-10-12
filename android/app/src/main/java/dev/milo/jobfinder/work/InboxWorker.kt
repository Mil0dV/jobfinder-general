package dev.milo.jobfinder.work

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import dev.milo.jobfinder.Notifications
import dev.milo.jobfinder.data.InboxScanner
import dev.milo.jobfinder.data.Mail
import dev.milo.jobfinder.data.ScanState
import dev.milo.jobfinder.data.Store
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Background inbox scan. Runs hourly through WorkManager — which sits on
 * JobScheduler, not Play Services, so it works on a de-Googled OS. Doze can
 * stretch the interval; the scan that runs when the app is opened covers the gap.
 */
class InboxWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!Mail.configured) return@withContext Result.success()
        // Shared instances, not fresh ones: the UI holds the same Store, and two
        // copies of an in-memory-cached, whole-file-rewritten JSON store lose
        // records (see Store.of).
        val store = Store.of(applicationContext.filesDir)
        val scanner = InboxScanner(store, ScanState.of(applicationContext.filesDir))

        val result = scanner.scan()
        if (result.toNotify.isNotEmpty()) {
            val sent = Notifications.notify(applicationContext, result.toNotify)
            store.replies.markNotified(sent)
        }
        // Push the new verdicts so the desktop tool sees them too. Best-effort:
        // a failed push just means the next sync carries them.
        runCatching { store.syncWithRemote() }

        // A scan that couldn't reach the server retries with backoff; one that ran
        // but fell back to heuristics is a success — the cursor stayed put, so the
        // next run re-classifies those messages properly.
        if (result.ran) Result.success() else Result.retry()
    }

    companion object {
        private const val PERIODIC = "inbox-scan-periodic"
        private const val ONE_OFF = "inbox-scan-now"

        private val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        /** Idempotent: safe to call on every launch. */
        fun schedule(context: Context) {
            if (!Mail.configured) return
            val request = PeriodicWorkRequestBuilder<InboxWorker>(1, TimeUnit.HOURS)
                .setConstraints(constraints)
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                PERIODIC,
                ExistingPeriodicWorkPolicy.KEEP,
                request,
            )
        }

        /** Scan right now — on app launch, and from the menu's "Check inbox". */
        fun runNow(context: Context) {
            if (!Mail.configured) return
            val request = OneTimeWorkRequestBuilder<InboxWorker>()
                .setConstraints(constraints)
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                ONE_OFF,
                ExistingWorkPolicy.REPLACE,
                request,
            )
        }
    }
}
