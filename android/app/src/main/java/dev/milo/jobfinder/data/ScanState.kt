package dev.milo.jobfinder.data

import org.json.JSONObject
import java.io.File

/**
 * Where the last inbox scan got to, plus the LLM back-off deadline — persisted
 * in mail_state.json next to the other stores in filesDir.
 *
 * [lastUid] is only advanced after a scan the classifier fully handled. A scan
 * that fell back to heuristics (no credits, API down) leaves the cursor where it
 * was, so the same mail is re-classified properly once the LLM is reachable
 * again; records are keyed by message id, so the better verdict just overwrites
 * the provisional one.
 */
class ScanState private constructor(dir: File) {

    companion object {
        @Volatile
        private var instance: ScanState? = null

        /** Process-wide instance — see [Store.of] for why a second one is a bug. */
        fun of(dir: File): ScanState = instance ?: synchronized(this) {
            instance ?: ScanState(dir).also { instance = it }
        }
    }

    private val file = File(dir, "mail_state.json")

    private val json: JSONObject = try {
        JSONObject(file.readText())
    } catch (e: Exception) {
        JSONObject()
    }

    var uidValidity: Long
        get() = json.optLong("uid_validity", 0L)
        set(v) { json.put("uid_validity", v) }

    var lastUid: Long
        get() = json.optLong("last_uid", 0L)
        set(v) { json.put("last_uid", v) }

    var lastScan: Long
        get() = json.optLong("last_scan", 0L)
        set(v) { json.put("last_scan", v) }

    /** Epoch millis until which LLM classification is paused (0 = available). */
    var llmPausedUntil: Long
        get() = json.optLong("llm_paused_until", 0L)
        set(v) { json.put("llm_paused_until", v) }

    /** Why it's paused, for the banner ("credit balance is too low"). */
    var llmPauseReason: String
        get() = json.optString("llm_pause_reason", "")
        set(v) { json.put("llm_pause_reason", v) }

    val llmPaused: Boolean
        get() = System.currentTimeMillis() < llmPausedUntil

    fun pauseLlm(reason: String, forMillis: Long) {
        llmPausedUntil = System.currentTimeMillis() + forMillis
        llmPauseReason = reason
        save()
    }

    fun resumeLlm() {
        if (llmPausedUntil == 0L && llmPauseReason.isEmpty()) return
        llmPausedUntil = 0L
        llmPauseReason = ""
        save()
    }

    fun save() {
        try {
            file.writeText(json.toString(2))
        } catch (e: Exception) { /* best-effort */ }
    }
}
