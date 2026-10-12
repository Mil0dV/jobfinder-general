package dev.milo.jobfinder.data

import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * One classified reply to an application. Stored per *email*, not per job: an
 * application often gets an acknowledgement first and an invitation later, and
 * keeping both preserves the history and makes dedup trivial (the document id is
 * the message id, so re-classifying overwrites in place).
 */
data class Reply(
    val id: String,
    val messageId: String,
    /** Matched job's cross-device key ("company|title"), or "" when unmatched. */
    val key: String,
    val company: String,
    val title: String,
    val status: String,
    val subject: String,
    val from: String,
    val ts: String,
    val snippet: String,
    val threadUrl: String,
    val confidence: Int,
    val needsReview: Boolean,
    val classifiedBy: String,
    val notified: Boolean,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("message_id", messageId)
        .put("key", key)
        .put("company", company)
        .put("title", title)
        .put("status", status)
        .put("subject", subject)
        .put("from", from)
        .put("ts", ts)
        .put("snippet", snippet)
        .put("thread_url", threadUrl)
        .put("confidence", confidence)
        .put("needs_review", needsReview)
        .put("classified_by", classifiedBy)
        .put("notified", notified)

    val isInvite: Boolean get() = status == ReplyStatus.INVITED

    /** The application is stalled until Milo does something (see [ReplyStatus.ACTION_NEEDED]). */
    val needsAction: Boolean get() = status == ReplyStatus.ACTION_NEEDED

    /** Worth a notification: an invitation, or a step that has to be finished. */
    val isActionable: Boolean get() = isInvite || needsAction

    /** "Enpal — Head of Product", or the subject when nothing matched. */
    val label: String
        get() = when {
            company.isNotBlank() && title.isNotBlank() -> "$company — $title"
            company.isNotBlank() -> company
            else -> subject.ifBlank { from }
        }

    companion object {
        fun fromJson(id: String, o: JSONObject): Reply = Reply(
            id = id,
            messageId = o.optString("message_id"),
            key = o.optString("key"),
            company = o.optString("company"),
            title = o.optString("title"),
            status = o.optString("status", ReplyStatus.OTHER),
            subject = o.optString("subject"),
            from = o.optString("from"),
            ts = o.optString("ts"),
            snippet = o.optString("snippet"),
            threadUrl = o.optString("thread_url"),
            confidence = o.optInt("confidence", 0),
            needsReview = o.optBoolean("needs_review", false),
            classifiedBy = o.optString("classified_by", "llm"),
            notified = o.optBoolean("notified", false),
        )

        private val TS_FORMAT: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm").withZone(ZoneId.systemDefault())

        fun formatTs(epochMillis: Long): String =
            TS_FORMAT.format(Instant.ofEpochMilli(epochMillis))

        /** Build a stored reply from a classified message. */
        fun of(header: MailHeader, verdict: Verdict, body: String): Reply = Reply(
            id = Sync.sha1(header.messageId),
            messageId = header.messageId,
            key = verdict.job?.key.orEmpty(),
            company = verdict.job?.company.orEmpty(),
            title = verdict.job?.title.orEmpty(),
            status = verdict.status,
            subject = header.subject,
            from = header.from,
            ts = formatTs(header.receivedAt),
            snippet = body.replace(Regex("\\s+"), " ").trim().take(200),
            threadUrl = header.threadUrl,
            confidence = verdict.confidence,
            needsReview = verdict.needsReview,
            classifiedBy = verdict.classifiedBy,
            notified = false,
        )
    }
}

/**
 * File-backed reply store (replies.json in filesDir), mirroring [Store]'s
 * approach. The Python side keeps a read-only copy in job_replies.json.
 */
class RepliesStore(dir: File) {

    private val file = File(dir, "replies.json")

    private val replies: JSONObject by lazy {
        try {
            JSONObject(file.readText())
        } catch (e: Exception) {
            JSONObject()
        }
    }

    fun all(): List<Reply> = replies.keys().asSequence()
        .mapNotNull { id -> replies.optJSONObject(id)?.let { Reply.fromJson(id, it) } }
        .sortedByDescending { it.ts }
        .toList()

    fun invites(): List<Reply> = all().filter { it.isInvite }

    fun needingAction(): List<Reply> = all().filter { it.needsAction }

    /** Invites and unfinished applications that have not produced a notification yet. */
    fun pendingNotifications(): List<Reply> = all().filter { it.isActionable && !it.notified }

    /**
     * Store a classified reply. Keeps the existing `notified` flag so a
     * re-classification (e.g. once API credits are back) can't buzz twice.
     * Returns the stored record.
     */
    fun upsert(reply: Reply): Reply {
        val previous = replies.optJSONObject(reply.id)
        val merged = if (previous != null && previous.optBoolean("notified", false)) {
            reply.copy(notified = true)
        } else {
            reply
        }
        replies.put(merged.id, merged.toJson())
        return merged
    }

    fun markNotified(ids: Collection<String>) {
        if (ids.isEmpty()) return
        for (id in ids) replies.optJSONObject(id)?.put("notified", true)
        save()
    }

    /** Merge remote records in, newest `ts` winning. Returns how many changed. */
    fun mergeRemote(remote: Map<String, JSONObject>): Int {
        var merged = 0
        for ((id, record) in remote) {
            val mine = replies.optJSONObject(id)
            if (mine == null || record.optString("ts") > mine.optString("ts")) {
                replies.put(id, record)
                merged++
            }
        }
        if (merged > 0) save()
        return merged
    }

    fun asJson(): JSONObject = replies

    fun save() {
        try {
            file.writeText(replies.toString(2))
        } catch (e: Exception) { /* best-effort */ }
    }
}
