package dev.milo.jobfinder.data

/**
 * Tells a spent API key apart from a blip.
 *
 * [Anthropic.complete] throws `RuntimeException("API <code>: <message>")`. A 401
 * (bad key), 402 / "credit balance too low" (out of money) or a sustained 429
 * won't fix itself in the next few minutes, so classification pauses for
 * [BACKOFF_MS] and the scan falls back to [Heuristics] — which is tuned to
 * over-report rather than miss an invite. A timeout or a 500 is transient: the
 * scan degrades the same way, but nothing is paused and the next run retries.
 */
object LlmBudget {

    /** How long to stop calling the API after a terminal-looking failure. */
    const val BACKOFF_MS = 6 * 60 * 60 * 1000L

    private val CODE = Regex("^api (\\d{3}):")

    private val TERMINAL_PHRASES = listOf(
        "credit balance",
        "insufficient",
        "quota",
        "billing",
        "payment",
        "invalid x-api-key",
        "authentication",
    )

    /** True when the failure means "further calls are pointless for a while". */
    fun isTerminal(e: Throwable): Boolean {
        val msg = e.message.orEmpty().lowercase()
        when (CODE.find(msg)?.groupValues?.get(1)?.toIntOrNull()) {
            401, 402, 403, 429 -> return true
        }
        return TERMINAL_PHRASES.any { it in msg }
    }

    /** Short human-readable cause for the "LLM paused" banner. */
    fun reason(e: Throwable): String {
        val msg = e.message.orEmpty()
        return when {
            "credit balance" in msg.lowercase() -> "out of API credits"
            CODE.find(msg.lowercase())?.groupValues?.get(1) == "401" -> "API key rejected"
            CODE.find(msg.lowercase())?.groupValues?.get(1) == "429" -> "rate limited"
            else -> msg.take(80).ifBlank { "API unavailable" }
        }
    }
}
