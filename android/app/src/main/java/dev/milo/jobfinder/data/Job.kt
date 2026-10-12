package dev.milo.jobfinder.data

import org.json.JSONObject
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

data class Job(
    val title: String = "",
    val company: String = "Unknown",
    val url: String = "",
    val location: String = "",
    val salary: String = "Not listed",
    val postedDate: String = "",
    val wfhPolicy: String = "Unknown",
    val snippet: String = "",
    val source: String = "",
    var fitScore: Int = -1,
    var startupScore: Int = -1,
    var climateScore: Int = -1,
    var reason: String = "",
    // Runtime-only: full posting text, fetched during search / lazily on the card.
    var desc: String? = null,
    var companyInfo: String? = null,
) {
    val normUrl: String get() = normalizeUrl(url)

    /**
     * Cross-device identity: company|title (lowercased), or the normalized URL
     * when there's no real company+title. MUST match norm_key in sync.py — the
     * dedup key, the Firestore doc id and the swipe-decision key are all this
     * string, so the same role from two sources (different URLs) is one job on
     * every device and decisions converge.
     */
    val decisionKey: String
        get() {
            val c = company.trim().lowercase()
            val t = title.trim().lowercase()
            if (c.isNotEmpty() && c != "unknown" && t.isNotEmpty()) return "$c|$t"
            return normUrl.ifEmpty { if (c.isNotEmpty() && t.isNotEmpty()) "$c|$t" else "" }
        }

    /** A "pearl" supermatch: strong on all three scores, with at least one 5. */
    val isPearl: Boolean
        get() = minOf(fitScore, startupScore, climateScore) >= 4 &&
            fitScore + startupScore + climateScore >= 13

    fun ageDays(): Long? {
        if (postedDate.isBlank()) return null
        return try {
            val d = LocalDate.parse(postedDate.take(10), DateTimeFormatter.ISO_LOCAL_DATE)
            ChronoUnit.DAYS.between(d, LocalDate.now(ZoneOffset.UTC))
        } catch (e: Exception) {
            null
        }
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("title", title)
        put("company", company)
        put("url", url)
        put("location", location)
        put("salary", salary)
        put("posted_date", postedDate)
        put("wfh_policy", wfhPolicy)
        put("snippet", snippet)
        put("source", source)
        put("fit_score", fitScore)
        put("startup_score", startupScore)
        put("climate_score", climateScore)
        put("reason", reason)
    }

    companion object {
        fun normalizeUrl(u: String?): String =
            (u ?: "").substringBefore("?").trim().trimEnd('/')

        fun fromJson(o: JSONObject): Job = make(
            title = o.optString("title"),
            company = o.optString("company", "Unknown"),
            url = o.optString("url"),
            location = o.optString("location"),
            salary = o.optString("salary", "Not listed"),
            postedDate = o.optString("posted_date"),
            wfhPolicy = o.optString("wfh_policy", "Unknown"),
            snippet = o.optString("snippet"),
            source = o.optString("source"),
        ).copy(
            fitScore = o.optInt("fit_score", -1),
            startupScore = o.optInt("startup_score", -1),
            climateScore = o.optInt("climate_score", -1),
            reason = o.optString("reason").takeUnless { it == "null" || it == "None" } ?: "",
        )

        // org.json renders JSON null as "null"; old jobs.csv rows carry Python's
        // "None". Scrub both everywhere.
        private fun clean(s: String?): String? =
            s?.trim()?.takeUnless { it == "null" || it == "None" || it.isEmpty() }

        fun make(
            title: String?,
            company: String?,
            url: String?,
            location: String? = null,
            salary: String? = null,
            postedDate: String? = null,
            wfhPolicy: String? = null,
            snippet: String? = null,
            source: String? = null,
        ) = Job(
            title = clean(title) ?: "",
            company = clean(company) ?: "Unknown",
            url = clean(url) ?: "",
            location = clean(location) ?: "",
            salary = clean(salary) ?: "Not listed",
            postedDate = clean(postedDate) ?: "",
            wfhPolicy = clean(wfhPolicy) ?: "Unknown",
            snippet = (clean(snippet) ?: "").take(500),
            source = clean(source) ?: "",
        )
    }
}
