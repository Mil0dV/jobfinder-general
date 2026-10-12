package dev.milo.jobfinder.data

import java.io.File

/**
 * One inbox scan: fetch what's new, work out which messages are replies to jobs
 * the user applied to, store the verdicts, and hand back the invites that still
 * need a notification.
 *
 * The failure policy throughout is "never lose an invitation":
 *  - the LLM decides normally, but any API failure falls back to [Heuristics]
 *    rather than skipping the message;
 *  - a degraded scan does not advance the UID cursor, so the same mail gets a
 *    proper verdict once the API is reachable again (records are keyed by
 *    message id, so the better verdict overwrites the provisional one);
 *  - notifications are tracked per message, so re-classifying never buzzes twice.
 */
class InboxScanner(
    private val store: Store,
    private val state: ScanState,
) {

    constructor(dir: File, store: Store) : this(store, ScanState.of(dir))

    data class Result(
        val scanned: Int = 0,
        val candidates: Int = 0,
        /**
         * Replies from this scan that deserve a notification and haven't had one:
         * interview invites, and applications stuck waiting on a step.
         */
        val toNotify: List<Reply> = emptyList(),
        /** True when some or all verdicts came from heuristics, not the LLM. */
        val degraded: Boolean = false,
        val skipped: Int = 0,
        val error: String? = null,
    ) {
        val ran: Boolean get() = error == null
    }

    companion object {
        /** How far back the very first scan looks. */
        const val BACKFILL_DAYS = 90

        /**
         * One scan at a time per process. The hourly worker and the screen's
         * "scan now" both drive this against the same [Store]; overlapping them
         * would mutate the same JSON objects from two threads and double-classify
         * the same page.
         */
        private val lock = Any()
    }

    fun scan(): Result = synchronized(lock) {
        if (!Mail.configured) {
            return Result(error = "Gmail not configured — set GMAIL_ADDRESS in Keys.kt")
        }
        val applied = store.appliedJobs()
        return try {
            Mail.withInbox { inbox -> scanInbox(inbox, applied) }
        } catch (e: Exception) {
            // Auth failures land here too; the message is shown on the menu.
            Result(error = e.message?.take(120) ?: "inbox scan failed")
        }
    }

    private fun scanInbox(inbox: Mail.Inbox, applied: List<AppliedJob>): Result {
        val validity = inbox.uidValidity
        // A changed uidValidity invalidates every stored UID — rescan by date.
        val since = if (state.uidValidity == validity) state.lastUid else 0L
        val page = inbox.headers(since, BACKFILL_DAYS)
        if (page.headers.isEmpty()) {
            state.uidValidity = validity
            state.lastScan = System.currentTimeMillis()
            state.save()
            return Result()
        }

        var degraded = false
        val useLlm = !state.llmPaused

        // --- pass 1: which of these could be about an application? ---
        val candidates = if (useLlm) {
            try {
                ReplyClassifier.triage(page.headers, applied).also { state.resumeLlm() }
            } catch (e: Exception) {
                degraded = true
                pauseIfTerminal(e)
                page.headers.filter { Heuristics.looksJobRelated(it, applied) }
            }
        } else {
            degraded = true
            page.headers.filter { Heuristics.looksJobRelated(it, applied) }
        }

        // --- pass 2: what does each one say? ---
        val notify = mutableListOf<Reply>()
        for (header in candidates) {
            val body = inbox.body(header.uid)
            val verdict = if (useLlm && !degraded) {
                try {
                    ReplyClassifier.classify(header, body, applied)
                } catch (e: Exception) {
                    degraded = true
                    pauseIfTerminal(e)
                    Heuristics.classify(header, body, applied)
                }
            } else {
                Heuristics.classify(header, body, applied)
            }
            val stored = store.replies.upsert(Reply.of(header, verdict, body))
            if (stored.isActionable && !stored.notified) notify.add(stored)
        }
        store.replies.save()

        // Only commit the cursor when the whole page was judged properly.
        if (!degraded) {
            state.uidValidity = validity
            state.lastUid = page.cursor
        }
        state.lastScan = System.currentTimeMillis()
        state.save()

        return Result(
            scanned = page.headers.size,
            candidates = candidates.size,
            toNotify = notify,
            degraded = degraded,
            skipped = page.skipped,
        )
    }

    private fun pauseIfTerminal(e: Exception) {
        if (LlmBudget.isTerminal(e)) {
            state.pauseLlm(LlmBudget.reason(e), LlmBudget.BACKOFF_MS)
        }
    }

    /** One-line status for the menu, or null when there's nothing to say. */
    fun statusLine(): String? {
        val invites = store.replies.invites().size
        val todo = store.replies.needingAction().size
        val paused = state.llmPaused
        val counts = listOfNotNull(
            "✉ $invites interview invite(s)".takeIf { invites > 0 },
            "$todo application(s) waiting on you".takeIf { todo > 0 },
        ).joinToString(" · ")
        return when {
            counts.isNotEmpty() && paused -> "$counts · unverified (${state.llmPauseReason})"
            counts.isNotEmpty() -> counts
            paused -> "⚠ reply classification paused — ${state.llmPauseReason}"
            else -> null
        }
    }
}
