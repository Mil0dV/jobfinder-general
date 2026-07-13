package dev.milo.jobfinder.data

/**
 * Candidate target-role profile — Kotlin port of candidate_profile.py plus the
 * fixed location policy for the app: Germany remote, Netherlands remote,
 * Switzerland hybrid/remote.
 */
object Profile {
    const val PROFILE_QUERY = "profile"

    val TARGET_ROLES = listOf(
        "Entrepreneur in Residence (EiR)",
        "Product Owner / Product Lead / Head of Product",
        "Technical Product Manager / Senior Technical Product Manager",
        "Platform Product Manager / Technical Product Lead (AI / platform)",
        "CTO / VP Engineering / Director of (Software) Engineering",
        "First Engineer / Founding Engineer / Tech Lead",
        "Principal / Staff Product Engineer",
    )

    val CANDIDATE_PROFILE = """
        Senior product & engineering leader looking for hands-on leadership roles at
        climate/green-tech startups and scale-ups. Target roles: ${TARGET_ROLES.joinToString("; ")}.
        Prefers early-stage or scale-up companies. Based in Switzerland; open to roles
        in Switzerland (hybrid or remote), or fully remote roles in Germany or the Netherlands.
    """.trimIndent()

    private val ROLE_PATTERNS = listOf(
        "entrepreneur[\\s-]+in[\\s-]+residence|\\beir\\b",
        "(head|director|vp|vice president) of (product|engineering|software|technology|tech)\\b",
        "chief (technology|technical|product) officer|\\bcto\\b|\\bcpo\\b",
        "(founding|first|principal|staff|lead) (software |full[\\s-]?stack |platform |ai |product )?engineer",
        "product (owner|lead|director)",
        "(technical|platform|senior|principal|staff|group|lead) product manager",
        "tech(nical)? lead",
        "engineering (director|lead|manager)",
        "(produkt(manager|leiter|owner)|technische[r]? leiter|leiter (produkt|entwicklung))",
    ).map { Regex(it, RegexOption.IGNORE_CASE) }

    private val EXCLUDE_PATTERNS = listOf(
        "\\b(intern|internship|werkstudent|praktik)",
        "\\bunpaid\\b|\\bvolunteer\\b",
        "(construction|electrical|mechanical|civil|hvac|hardware|process|sales|marketing)\\s+(engineer|manager|lead)\\b",
        "art director",
    ).map { Regex(it, RegexOption.IGNORE_CASE) }

    val PROFILE_QUERY_SEEDS = listOf(
        "head of product",
        "CTO",
        "technical product manager",
        "director of engineering",
        "founding engineer",
        "product lead",
        "staff product engineer",
        "entrepreneur in residence",
    )

    fun matchesTargetRoles(title: String): Boolean {
        if (title.isBlank()) return false
        if (EXCLUDE_PATTERNS.any { it.containsMatchIn(title) }) return false
        return ROLE_PATTERNS.any { it.containsMatchIn(title) }
    }

    /** Match a job title against a free-text query, or the role profile. */
    fun titleMatches(title: String, query: String): Boolean {
        if (query == PROFILE_QUERY || query.isBlank()) return matchesTargetRoles(title)
        val q = query.lowercase()
        val t = title.lowercase()
        if ("," in q) return q.split(",").any { it.trim() in t }
        return q.split(Regex("\\s+")).all { it in t }
    }

    // --- Location policy -----------------------------------------------------

    private val SWISS_TERMS = listOf(
        "switzerland", "schweiz", "suisse", "zurich", "zürich", "geneva", "genf",
        "basel", "bern", "lausanne", "zug",
    )
    private val GERMAN_TERMS = listOf(
        "germany", "deutschland", "berlin", "munich", "münchen", "hamburg",
        "frankfurt", "cologne", "köln", "stuttgart", "leipzig",
    )
    private val DUTCH_TERMS = listOf(
        "netherlands", "nederland", "amsterdam", "rotterdam", "utrecht",
        "the hague", "eindhoven",
    )
    private val EUROPE_TERMS = listOf(
        "europe", "emea", "european", "eu remote", "remote eu", "cet", "worldwide",
        "global", "anywhere",
    )
    private val REMOTE_HINTS = listOf(
        "remote", "anywhere", "distributed", "work from home", "home office",
    )
    // A remote job restricted to one of these (with no EU-target term) can't hire in DE/NL.
    private val NON_EU_MARKERS = listOf(
        "united states", "usa", "u.s.", "us only", "us-only", "north america",
        "americas", "canada", "latam", "india", "australia", "new zealand",
        "apac", "asia", "africa", "united kingdom", "uk only",
    )

    fun isRemote(job: Job): Boolean {
        val hay = (job.location + " " + job.wfhPolicy).lowercase()
        return REMOTE_HINTS.any { it in hay }
    }

    /**
     * Milo's location policy: Switzerland any (hybrid/on-site/remote); Germany and
     * the Netherlands only if the role is remote; other remote-EU/worldwide roles pass;
     * remote roles clearly restricted to non-EU regions don't.
     */
    fun matchesLocations(job: Job): Boolean {
        val loc = job.location.lowercase()
        if (SWISS_TERMS.any { it in loc }) return true
        if (!isRemote(job)) {
            // Non-remote outside Switzerland is out — hybrid is only OK for CH.
            return false
        }
        if ((GERMAN_TERMS + DUTCH_TERMS + EUROPE_TERMS).any { it in loc }) return true
        if (NON_EU_MARKERS.any { it in loc }) return false
        return true // plain "Remote" with no restriction — assume reachable
    }
}
