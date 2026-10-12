package dev.milo.jobfinder

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import dev.milo.jobfinder.data.Reply

/**
 * Notifications for the two replies that need Milo, on two channels so they can
 * be tuned apart: an interview invitation (high), and an application that can't
 * proceed until he answers screening questions or re-uploads something
 * (default — real, but not worth a buzz mid-meeting). Rejections and
 * acknowledgements never notify; they're read in the replies list.
 */
object Notifications {

    const val CHANNEL_ID = "interviews"
    const val CHANNEL_ACTIONS = "applications"
    const val EXTRA_OPEN_REPLIES = "open_replies"

    private fun channels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Interview invites",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "Replies to your applications that invite you to talk"
            }
        )
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ACTIONS,
                "Applications waiting on you",
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = "Screening questions, assessments or documents still to submit"
            }
        )
    }

    private fun allowed(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * Post one notification per reply. Returns the ids that actually went out,
     * so the caller only marks those as notified — if the permission is missing,
     * nothing is marked and the reply still shows up in the app (and buzzes once
     * the permission is granted).
     */
    fun notify(context: Context, replies: List<Reply>): List<String> {
        if (replies.isEmpty() || !allowed(context)) return emptyList()
        channels(context)
        val manager = NotificationManagerCompat.from(context)
        val sent = mutableListOf<String>()
        for (invite in replies) {
            val intent = Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_OPEN_REPLIES, true)
            }
            val pending = PendingIntent.getActivity(
                context,
                invite.id.hashCode(),
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val action = invite.needsAction
            val title = when {
                action -> "Application needs a step"
                invite.needsReview -> "Possible interview invite"
                else -> "Interview invite"
            }
            val notification = NotificationCompat.Builder(
                context,
                if (action) CHANNEL_ACTIONS else CHANNEL_ID,
            )
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setContentTitle("$title — ${invite.label}")
                .setContentText(invite.subject)
                .setStyle(NotificationCompat.BigTextStyle().bigText(invite.snippet))
                .setPriority(
                    if (action) NotificationCompat.PRIORITY_DEFAULT
                    else NotificationCompat.PRIORITY_HIGH
                )
                .setAutoCancel(true)
                .setContentIntent(pending)
                .build()
            try {
                manager.notify(invite.id.hashCode(), notification)
                sent.add(invite.id)
            } catch (e: SecurityException) {
                return sent
            }
        }
        return sent
    }
}
