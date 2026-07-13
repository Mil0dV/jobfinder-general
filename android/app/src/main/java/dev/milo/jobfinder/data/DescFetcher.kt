package dev.milo.jobfinder.data

import org.json.JSONObject
import java.io.File

/**
 * Job-description fetching: download the posting page and extract readable text
 * (a lightweight stand-in for trafilatura), with an on-disk cache like
 * job_descriptions.json in the Python version.
 */
class DescFetcher(private val cacheFile: File) {

    private val cache: JSONObject by lazy {
        try {
            JSONObject(cacheFile.readText())
        } catch (e: Exception) {
            JSONObject()
        }
    }
    private val lock = Any()

    fun fetch(url: String): String {
        val key = Job.normalizeUrl(url)
        if (key.isEmpty()) return ""
        synchronized(lock) {
            cache.optJSONObject(key)?.let { entry ->
                val desc = entry.optString("description")
                if (desc.isNotBlank()) return desc // fetched once, re-used forever
                val fetchedAt = entry.optLong("fetched_ms", 0)
                if (System.currentTimeMillis() - fetchedAt < FAILED_RETRY_MS) return ""
                // stale failure: fall through and retry
            }
        }

        // Another device may have fetched this posting already.
        Sync.getDescription(url)?.let { remote ->
            store(key, remote)
            return remote
        }

        var text = Http.get(url)?.let { extractText(it) } ?: ""
        if (text.length < 200) {
            // Hard-to-fetch page (bot wall / JS-only shell): r.jina.ai renders
            // it and returns plain text.
            val viaJina = Http.get("https://r.jina.ai/$url", mapOf("X-Return-Format" to "text"))
            if (viaJina != null && viaJina.trim().length > 200) text = viaJina.trim()
        }

        store(key, text) // empty = negative cache entry
        if (text.isNotBlank()) Sync.pushDescription(url, text)
        return text
    }

    private fun store(key: String, text: String) {
        synchronized(lock) {
            cache.put(
                key,
                JSONObject()
                    .put("description", text)
                    .put("fetched_ms", System.currentTimeMillis()),
            )
            try {
                cacheFile.writeText(cache.toString())
            } catch (e: Exception) { /* cache write is best-effort */ }
        }
    }

    companion object {
        // Failed fetches are cached too (empty description) and only retried
        // this often, so unfetchable postings don't refetch on every deck view.
        private const val FAILED_RETRY_MS = 7L * 24 * 3600 * 1000

        /** Strip boilerplate tags and boil HTML down to readable text. */
        fun extractText(html: String): String {
            var s = html
            // Drop non-content sections entirely.
            for (tag in listOf("script", "style", "noscript", "svg", "nav", "header", "footer", "form")) {
                s = s.replace(Regex("(?is)<$tag\\b.*?</$tag>"), " ")
            }
            s = s.replace(Regex("(?s)<!--.*?-->"), " ")
            // Block-level tags become newlines so paragraphs survive.
            s = s.replace(Regex("(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)[^>]*>"), "\n")
            s = s.replace(Regex("(?i)<li[^>]*>"), "\n- ")
            s = s.replace(Regex("<[^>]+>"), " ")
            // Entities we actually meet in postings.
            s = s.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", "\"").replace("&#39;", "'").replace("&apos;", "'")
                .replace("&nbsp;", " ").replace(Regex("&#(\\d+);")) {
                    it.groupValues[1].toIntOrNull()?.toChar()?.toString() ?: " "
                }
            // Some sites embed HTML-escaped markup in the page; decoding entities
            // can resurrect tags, so convert/strip once more.
            s = s.replace(Regex("(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/ul)[^>]*>"), "\n")
            s = s.replace(Regex("(?i)<li[^>]*>"), "\n- ")
            s = s.replace(Regex("<[^>]+>"), " ")
            s = s.replace("&amp;", "&")
            // Collapse whitespace, drop empty "-" bullets, keep paragraph breaks.
            s = s.lineSequence()
                .map { it.replace(Regex("\\s+"), " ").trim() }
                .filterNot { it == "-" }
                .joinToString("\n")
                .replace(Regex("\n{3,}"), "\n\n")
                .trim()
            return s
        }
    }
}
