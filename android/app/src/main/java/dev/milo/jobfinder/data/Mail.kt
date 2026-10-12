package dev.milo.jobfinder.data

import com.sun.mail.imap.IMAPFolder
import java.util.Date
import java.util.Properties
import javax.mail.FetchProfile
import javax.mail.Folder
import javax.mail.Message
import javax.mail.Multipart
import javax.mail.Part
import javax.mail.Session
import javax.mail.UIDFolder
import javax.mail.internet.InternetAddress
import javax.mail.internet.MimeMessage
import javax.mail.search.ComparisonTerm
import javax.mail.search.ReceivedDateTerm
import javax.mail.Store as MailStore

/** One inbox message's headers — everything the triage pass sees. */
data class MailHeader(
    val uid: Long,
    val messageId: String,
    /** Sender display name, e.g. "Jane Doe" or "Enpal Recruiting" ("" when absent). */
    val fromName: String,
    /** Sender address, e.g. "no-reply@greenhouse.io". */
    val fromAddress: String,
    val subject: String,
    /** Epoch millis the message was received. */
    val receivedAt: Long,
) {
    /** "Jane Doe <jane@acme.com>" — what the classifier prompt and the UI show. */
    val from: String
        get() = if (fromName.isBlank()) fromAddress else "$fromName <$fromAddress>"

    /** The sender's domain, lowercased and stripped of a leading "mail."/"e."-style label. */
    val domain: String
        get() = fromAddress.substringAfter('@', "").lowercase()

    /** Gmail deep link that opens this exact message. */
    val threadUrl: String
        get() = "https://mail.google.com/mail/u/0/#search/rfc822msgid:" +
            messageId.trim().removePrefix("<").removeSuffix(">")
}

/**
 * One page of headers plus the UID the caller should store once it has finished
 * with them. [skipped] is how many matching messages this page left behind — 0
 * in the steady state; non-zero only while a backlog drains or on the first run.
 */
data class HeaderPage(val headers: List<MailHeader>, val cursor: Long, val skipped: Int)

/**
 * Gmail over IMAP, using JavaMail's Android port — pure JVM, so it needs no
 * Google Play Services and works on a de-Googled OS. Auth is an app password
 * (Keys.GMAIL_APP_PASSWORD); blank credentials disable inbox scanning entirely.
 *
 * All calls block — run them on Dispatchers.IO.
 */
object Mail {

    private const val HOST = "imap.gmail.com"
    private const val PORT = 993

    /** Max characters of body text handed to the classifier. */
    private const val BODY_LIMIT = 6000

    val configured: Boolean
        get() = Keys.GMAIL_ADDRESS.isNotBlank() && Keys.GMAIL_APP_PASSWORD.isNotBlank()

    /**
     * Connect, open INBOX read-only, hand it to [block], then close both. One
     * connection serves a whole scan (headers first, then the bodies triage asked
     * for), so a scan costs a single login.
     */
    fun <T> withInbox(block: (Inbox) -> T): T {
        val props = Properties().apply {
            put("mail.store.protocol", "imaps")
            put("mail.imaps.host", HOST)
            put("mail.imaps.port", PORT.toString())
            put("mail.imaps.ssl.enable", "true")
            put("mail.imaps.connectiontimeout", "20000")
            put("mail.imaps.timeout", "30000")
        }
        val session = Session.getInstance(props)
        val store: MailStore = session.getStore("imaps")
        // Google shows app passwords in 4-character groups; the spaces are cosmetic.
        store.connect(HOST, PORT, Keys.GMAIL_ADDRESS, Keys.GMAIL_APP_PASSWORD.replace(" ", ""))
        try {
            val folder = store.getFolder("INBOX") as IMAPFolder
            folder.open(Folder.READ_ONLY)
            try {
                return block(Inbox(folder))
            } finally {
                runCatching { folder.close(false) }
            }
        } finally {
            runCatching { store.close() }
        }
    }

    /** An open INBOX. Only valid inside [withInbox]. */
    class Inbox internal constructor(private val folder: IMAPFolder) {

        /**
         * IMAP's generation counter for this mailbox. If it changes, every stored
         * UID is meaningless and the caller must re-scan by date instead.
         */
        val uidValidity: Long
            get() = folder.uidValidity

        /**
         * Headers of mail newer than [sinceUid]. When [sinceUid] is 0 (first run, or
         * after a uidValidity change) it falls back to everything received in the
         * last [backfillDays] instead.
         *
         * At most [limit] messages come back per call, and which end of the queue
         * gets dropped differs by design:
         *
         *  - incremental (sinceUid > 0): the *oldest* unseen messages, so the
         *    cursor advances without stepping over anything. A backlog drains over
         *    consecutive scans instead of being skipped.
         *  - first run (sinceUid == 0): the *newest* in the window, because that's
         *    where a live invitation would be, and [HeaderPage.cursor] jumps past
         *    the whole window so the older tail is never revisited. Anything that
         *    old has already been dealt with by hand.
         */
        fun headers(sinceUid: Long, backfillDays: Int, limit: Int = 400): HeaderPage {
            val backfill = sinceUid <= 0
            val messages: Array<Message> = if (!backfill) {
                folder.getMessagesByUID(sinceUid + 1, UIDFolder.LASTUID)
                    ?.filterNotNull()?.toTypedArray() ?: emptyArray()
            } else {
                val cutoff = Date(System.currentTimeMillis() - backfillDays * 86_400_000L)
                folder.search(ReceivedDateTerm(ComparisonTerm.GE, cutoff)) ?: emptyArray()
            }
            if (messages.isEmpty()) return HeaderPage(emptyList(), sinceUid, 0)

            // One round trip for envelopes + UIDs instead of one per message.
            val profile = FetchProfile().apply {
                add(FetchProfile.Item.ENVELOPE)
                add(UIDFolder.FetchProfileItem.UID)
            }
            runCatching { folder.fetch(messages, profile) }

            val all = messages.mapNotNull { toHeader(it) }
                .filter { it.uid > sinceUid }
                .sortedBy { it.uid }
            if (all.isEmpty()) return HeaderPage(emptyList(), sinceUid, 0)

            val page = if (backfill) all.takeLast(limit) else all.take(limit)
            val cursor = if (backfill) all.last().uid else page.last().uid
            return HeaderPage(page, cursor, all.size - page.size)
        }

        /** Plain-text body for one message, truncated to a classifier-sized excerpt. */
        fun body(uid: Long): String {
            val msg = runCatching { folder.getMessageByUID(uid) }.getOrNull() ?: return ""
            val text = runCatching { extractText(msg) }.getOrDefault("")
            return text.replace(Regex("\\n{3,}"), "\n\n").trim().take(BODY_LIMIT)
        }

        private fun toHeader(msg: Message): MailHeader? {
            val uid = runCatching { folder.getUID(msg) }.getOrNull() ?: return null
            val sender = runCatching { msg.from?.firstOrNull() as? InternetAddress }.getOrNull()
            val messageId = runCatching { (msg as? MimeMessage)?.messageID }.getOrNull()
            val received = runCatching { msg.receivedDate ?: msg.sentDate }.getOrNull()
            return MailHeader(
                uid = uid,
                // Synthetic fallback keeps the document id stable for the rare
                // message that arrives without a Message-ID header.
                messageId = messageId?.takeIf { it.isNotBlank() }
                    ?: "uid-$uid-${uidValidity}@jobfinder.local",
                fromName = sender?.personal.orEmpty(),
                fromAddress = sender?.address.orEmpty(),
                subject = runCatching { msg.subject }.getOrNull().orEmpty(),
                receivedAt = received?.time ?: System.currentTimeMillis(),
            )
        }
    }

    // --- MIME text extraction ---------------------------------------------------

    /** Depth-first walk preferring text/plain, falling back to stripped HTML. */
    private fun extractText(part: Part, depth: Int = 0): String {
        if (depth > 6) return ""
        if (part.isMimeType("text/plain")) return part.content as? String ?: ""
        if (part.isMimeType("text/html")) return stripHtml(part.content as? String ?: "")
        val content = runCatching { part.content }.getOrNull()
        if (content is Multipart) {
            val parts = (0 until content.count).mapNotNull {
                runCatching { content.getBodyPart(it) }.getOrNull()
            }
            // multipart/alternative carries the same message twice — take the plain one.
            if (part.isMimeType("multipart/alternative")) {
                parts.firstOrNull { it.isMimeType("text/plain") }?.let {
                    val t = extractText(it, depth + 1)
                    if (t.isNotBlank()) return t
                }
            }
            return parts.joinToString("\n") { extractText(it, depth + 1) }.trim()
        }
        return ""
    }

    private fun stripHtml(html: String): String = html
        .replace(Regex("(?is)<(script|style|head)[^>]*>.*?</\\1>"), " ")
        .replace(Regex("(?i)<br\\s*/?>|</p>|</div>|</tr>"), "\n")
        .replace(Regex("<[^>]+>"), " ")
        .replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace(Regex("[ \\t]{2,}"), " ")
}
