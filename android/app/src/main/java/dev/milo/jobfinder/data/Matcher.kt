package dev.milo.jobfinder.data

/** A job the candidate marked as applied — what a reply can be about. */
data class AppliedJob(val key: String, val company: String, val title: String) {
    /** Normalized company name, e.g. "1Komma5° GmbH" → "1komma5". */
    val companyKey: String get() = Matcher.companyKey(company)
}

/**
 * Decides which applied job an email is a reply to.
 *
 * Replies rarely come from the company's own domain — ATS platforms send as
 * `no-reply@greenhouse.io` with the company in the display name, and recruiters
 * mail from personal addresses — so sender domain, sender name, subject and body
 * are each scored and the best-scoring applied job wins. Deliberately lenient:
 * an unmatched invite still gets stored and still notifies (tagged unmatched),
 * because a mislabelled invite is recoverable and a missed one is not.
 */
object Matcher {

    /** Applicant-tracking hosts that send on a company's behalf. */
    val ATS_DOMAINS = setOf(
        "greenhouse.io",
        "us.greenhouse-mail.io",
        "lever.co",
        "hire.lever.co",
        "ashbyhq.com",
        "personio.de",
        "personio.com",
        "recruitee.com",
        "recruiteemail.com",
        "recruitee-mail.com",
        "hipeople.io",
        "emails.hipeople.io",
        "workable.com",
        "smartrecruiters.com",
        "workday.com",
        "myworkday.com",
        "successfactors.com",
        "teamtailor.com",
        "join.com",
        "softgarden.io",
        "bamboohr.com",
    )

    /** Legal suffixes and filler, mirroring connections.py `_SUFFIXES`. */
    private val SUFFIXES = setOf(
        "gmbh", "ag", "ug", "kg", "mbh", "bv", "nv", "cv", "inc", "incorporated",
        "ltd", "limited", "llc", "llp", "plc", "sa", "sas", "sarl", "srl", "spa",
        "oy", "ab", "as", "aps", "holding", "holdings", "group", "the", "co",
        "corp", "corporation", "company",
    )

    /**
     * Normalize a company name: lowercase, punctuation and legal suffixes gone.
     * The Kotlin twin of connections.py `company_key`, so both sides group the
     * same spellings together.
     */
    fun companyKey(name: String): String {
        val text = name.lowercase()
            .replace(Regex("[^\\w\\s&+-]"), " ")
            .replace("&", " and ")
        val words = text.split(Regex("[\\s\\-_/]+")).filter { it.isNotBlank() }.toMutableList()
        while (words.isNotEmpty() && words.last() in SUFFIXES) words.removeAt(words.size - 1)
        while (words.isNotEmpty() && words.first() in SUFFIXES) words.removeAt(0)
        return words.joinToString(" ").trim()
    }

    /** The name squashed to letters and digits, for domain comparison ("1komma5"). */
    private fun squash(companyKey: String): String =
        companyKey.replace(Regex("[^a-z0-9]"), "")

    /**
     * The distinctive part of a name, for matching against a domain.
     *
     * [companyKey] only strips suffixes it recognises as whole words, so
     * "Overstory B.V." normalizes to "overstory b v" — which appears nowhere in
     * `overstory.com`. Falling back to the leading token (when it is long enough
     * to be distinctive) is what makes that mail match.
     */
    private fun leadToken(companyKey: String): String {
        val first = companyKey.substringBefore(' ')
        return if (first.length >= 4) first else companyKey
    }

    /** Does [haystack] contain this company under either of its forms? */
    private fun mentions(haystack: String, key: String): Boolean {
        if (key in haystack) return true
        val squashedHay = squash(haystack)
        return squash(key) in squashedHay || squash(leadToken(key)) in squashedHay
    }

    /**
     * Best applied-job match for a message, or null when nothing scores.
     * [body] may be empty — sender and subject alone are often enough.
     */
    fun match(header: MailHeader, body: String, applied: List<AppliedJob>): AppliedJob? {
        if (applied.isEmpty()) return null
        val domain = header.domain
        val fromName = header.fromName.lowercase()
        val subject = header.subject.lowercase()
        val bodyText = body.lowercase()

        var best: AppliedJob? = null
        var bestScore = 0

        for (job in applied) {
            val key = job.companyKey
            if (key.isBlank()) continue
            val squashed = squash(key)
            if (squashed.length < 3) continue // "ex", "hy" would match anything

            var score = 0
            // Sender domain is the strongest signal: mail from enpal.de about Enpal.
            if (mentions(domain.substringBeforeLast('.'), key)) score += 60
            // ATS and recruiter mail puts the company in the display name.
            if (mentions(fromName, key)) score += 45
            if (mentions(subject, key)) score += 30
            // Title in the subject line disambiguates two roles at one company.
            if (job.title.isNotBlank() && job.title.lowercase() in subject) score += 20
            if (bodyText.isNotEmpty() && mentions(bodyText, key)) score += 15
            // Longer names matching are less likely to be coincidence.
            if (score > 0) score += minOf(squashed.length, 12)

            if (score > bestScore) {
                bestScore = score
                best = job
            }
        }
        return if (bestScore >= 30) best else null
    }
}
