package dev.milo.jobfinder.data

import org.json.JSONArray
import org.json.JSONObject

/** What a reply says about an application. */
object ReplyStatus {
    const val INVITED = "invited"
    /**
     * The application is not finished until you do something: answer screening
     * questions, re-upload a CV, confirm an address. Separate from [INVITED]
     * because the deadline is real but nobody is offering to talk yet — and
     * separate from [ACKNOWLEDGED] because ignoring it kills the application.
     */
    const val ACTION_NEEDED = "action_needed"
    const val REJECTED = "rejected"
    const val ACKNOWLEDGED = "acknowledged"
    const val OTHER = "other"
}

/** One classified message, before it becomes a stored [Reply]. */
data class Verdict(
    val status: String,
    val job: AppliedJob?,
    val confidence: Int,
    val classifiedBy: String,
) {
    /** Anything provisional gets surfaced for a human glance. */
    val needsReview: Boolean
        get() = classifiedBy == "heuristic" || confidence < 60
}

/**
 * Two-pass reply classification.
 *
 * Pass 1 (triage) sends every new sender+subject in one call and keeps whatever
 * might be about an application — a couple of dozen subject lines is a tiny
 * prompt, so there is no keyword prefilter in the normal path to silently drop
 * an oddly-worded invite. Pass 2 fetches the body for those and decides the
 * status.
 *
 * Both passes are recall-first: the prompts say so explicitly, and every
 * failure path lands on [Heuristics], which over-reports rather than miss an
 * invite. A missed interview invite is the failure this whole feature exists to
 * prevent; a false positive costs one glance at a list.
 */
object ReplyClassifier {

    private const val TRIAGE_SYSTEM =
        "You screen an inbox for mail related to job applications the user has submitted.\n" +
            "You are given numbered messages (sender and subject only).\n" +
            "Return the indices of every message that could plausibly be about a job " +
            "application, a recruiter conversation, or an interview — including " +
            "applicant-tracking system mail, calendar invitations from a company, " +
            "requests to complete or finish an application (screening questions, " +
            "assessments, a missing document), and anything ambiguous.\n" +
            "Exclude only what is clearly unrelated: newsletters, marketing, invoices, " +
            "social notifications, job-board alerts advertising new vacancies.\n" +
            "When in doubt, INCLUDE the message. Missing a real interview invitation is " +
            "far worse than including a few irrelevant ones."

    private val TRIAGE_SCHEMA = JSONObject(
        """
        {"type":"object",
         "properties":{"candidates":{"type":"array","items":{"type":"integer"}}},
         "required":["candidates"],
         "additionalProperties":false}
        """.trimIndent()
    )

    private const val CLASSIFY_SYSTEM =
        "You read one email and decide what it says about a job application.\n" +
            "status:\n" +
            "  invited      — any invitation or request to talk: interview, intro call, " +
            "screening, scheduling/availability request, a booking link, a calendar " +
            "invitation, or moving to a next round.\n" +
            "  action_needed — the application is incomplete or on hold until the user " +
            "does something: answer screening questions, take an assessment or test, " +
            "re-upload a CV, fill in a form, confirm details. Typical wording: " +
            "\"complete your application\", \"one more step\", \"please answer these " +
            "questions\", \"finalize your application\". No human has offered to talk " +
            "yet — but ignoring it means the application never lands.\n" +
            "  rejected     — the application was turned down.\n" +
            "  acknowledged — application received / under review, no decision yet, " +
            "nothing for the user to do.\n" +
            "  other        — related to work or recruiting but none of the above " +
            "(e.g. cold outreach about a role the user never applied to).\n" +
            "If the mail both thanks the user for an interview AND declines them, that is " +
            "rejected. If it declines one role but proposes talking about another, that is " +
            "invited.\n" +
            "A task the user must complete is action_needed even when the link or the " +
            "sending system calls itself an \"invite\" (assessment invites, questionnaire " +
            "invites). Only a request to talk to a person is invited.\n" +
            "job_index: the index of the matching applied job, or -1 if none matches.\n" +
            "confidence: 0-100, how sure you are of the status.\n" +
            "When genuinely torn between invited and anything else, choose invited."

    private val CLASSIFY_SCHEMA = JSONObject(
        """
        {"type":"object",
         "properties":{
           "status":{"type":"string",
                     "enum":["invited","action_needed","rejected","acknowledged","other"]},
           "job_index":{"type":"integer"},
           "confidence":{"type":"integer"}},
         "required":["status","job_index","confidence"],
         "additionalProperties":false}
        """.trimIndent()
    )

    /**
     * Which of [headers] might be about an application. Throws on API failure —
     * the caller decides whether that means "pause and use heuristics".
     */
    fun triage(headers: List<MailHeader>, applied: List<AppliedJob>): List<MailHeader> {
        if (headers.isEmpty()) return emptyList()
        val listing = headers.mapIndexed { i, h ->
            "$i. from: ${h.from}\n   subject: ${h.subject}"
        }.joinToString("\n")
        val companies = applied.map { it.company }.distinct().take(60)
        val context = if (companies.isEmpty()) "" else
            "\n\nThe user has applied to jobs at: ${companies.joinToString(", ")}."

        val raw = Anthropic.complete(
            model = Anthropic.FAST_MODEL,
            maxTokens = 2048,
            system = TRIAGE_SYSTEM,
            user = "Messages:\n$listing$context",
            outputSchema = TRIAGE_SCHEMA,
        )
        val picked = JSONObject(raw).optJSONArray("candidates") ?: JSONArray()
        val indices = (0 until picked.length()).map { picked.optInt(it, -1) }.toSet()
        return headers.filterIndexed { i, _ -> i in indices }
    }

    /** Status for one candidate message. Throws on API failure. */
    fun classify(
        header: MailHeader,
        body: String,
        applied: List<AppliedJob>,
    ): Verdict {
        val jobList = applied.mapIndexed { i, j -> "$i. ${j.company} — ${j.title}" }
            .joinToString("\n")
            .ifBlank { "(none recorded)" }

        val raw = Anthropic.complete(
            model = Anthropic.FAST_MODEL,
            maxTokens = 512,
            system = CLASSIFY_SYSTEM,
            user = "Jobs the user applied to:\n$jobList\n\n" +
                "Email:\nfrom: ${header.from}\nsubject: ${header.subject}\n\n" +
                body.ifBlank { "(body unavailable)" },
            outputSchema = CLASSIFY_SCHEMA,
        )
        val out = JSONObject(raw)
        val status = out.optString("status", ReplyStatus.OTHER)
        val idx = out.optInt("job_index", -1)
        val job = applied.getOrNull(idx) ?: Matcher.match(header, body, applied)
        return Verdict(
            status = status,
            job = job,
            confidence = out.optInt("confidence", 50).coerceIn(0, 100),
            classifiedBy = "llm",
        )
    }
}

/**
 * The no-LLM fallback: pattern rules in English, German and Dutch, weighted so a
 * single strong invitation phrase wins unless an equally strong rejection phrase
 * outweighs it. Used when the API key is spent or unreachable. Pure Kotlin, no
 * Android or network dependencies, so the fixture tests exercise it directly.
 */
object Heuristics {

    private val STRONG_INVITE = listOf(
        // Note: no "move forward with your application" here — rejections say
        // "will not be moving forward with your application", and a near-miss on
        // the negation would turn a no into a yes.
        "invite you", "would like to invite", "invitation to interview",
        "you've been invited", "you have been invited", "invited to",
        "video interview", "phone interview", "first interview", "interview with",
        "like to meet", "like to schedule", "schedule a call", "schedule a short call",
        "schedule an interview", "schedule some time",
        "book a time", "book a slot", "book time with", "find a time",
        "set up a call", "set up a chat", "arrange a call", "reschedule",
        "your availability", "are you available", "when are you available",
        "next round", "next stage", "speak with you", "get to know you",
        // Booking-page links: the invitation is the link, not the prose.
        "calendly.com", "cal.com/", "savvycal", "hubspot.com/meetings", "bookwithme",
        "meet and greet", "cultural fit call", "intro call", "screening call",
        "einladen", "einladung", "eingeladen", "gespräch vereinbaren", "kennenlernen",
        "terminvorschlag", "verfügbarkeit", "vorstellungsgespräch", "gesprächstermin",
        "erstgespräch", "kennenlerngespräch",
        // A German invitation is often just "pick a slot in my calendar".
        "wähle dafür", "freien termin", "termin in meinem kalender", "kalender aus",
        "uitnodigen", "uitnodiging", "kennismakingsgesprek", "beschikbaarheid",
    )

    /**
     * Phrases that put an interview in the *conditional* — the hallmark of an
     * acknowledgement, not an invitation ("if you are selected for an interview,
     * a member of our team will be in touch"). Without this, every ATS
     * confirmation mail reads as an invite, because they all mention interviews.
     */
    private val CONDITIONAL_INTERVIEW = listOf(
        "if you are selected", "if you're selected", "should you be selected",
        "if we find your profile", "if your profile matches", "if your application",
        "should your profile match", "if your profile is a match",
        "if there is a match", "if we decide to move forward",
        "sollte dein profil", "sollte ihr profil", "falls wir",
    )

    /** Automated transactional mail from a booking page — never an invitation. */
    private val TRANSACTIONAL = listOf(
        "verification code", "verify your email", "confirm your email address",
        "bestätigungscode", "password reset",
    )

    /**
     * The application is stuck until the user does something. These are written to
     * beat both ACK ("thanks for applying" is usually in the same mail) and the
     * bare word "invite" — ATS vendors send assessment links from URLs like
     * `/candidate/assessment/invite`, and the sender genuinely calls it an invite.
     */
    private val ACTION_NEEDED = listOf(
        "complete your application", "complete the application", "completing your application",
        "finalize your application", "finalise your application", "finish your application",
        "continue your application", "complete your profile", "one more step", "one last step",
        "final step", "action required", "action needed", "your application is incomplete",
        "incomplete application", "questions to apply", "please complete", "please answer",
        "please upload", "upload your resume", "upload your cv", "re-upload",
        "a few more questions", "additional questions", "screening questions",
        "please fill in", "fill out the form", "complete the assessment", "take the assessment",
        "complete this assessment", "complete the questionnaire", "before we can proceed",
        "bewerbung abschließen", "bewerbung vervollständigen", "noch ein schritt",
        "fehlende unterlagen", "bitte ergänzen", "bitte lade", "bitte laden sie",
        "sollicitatie afronden", "sollicitatie voltooien", "nog één stap", "nog een stap",
    )

    private val WEAK_INVITE = listOf(
        "interview", "next steps", "call", "meeting", "chat", "gespräch", "gesprek",
    )

    private val REJECT = listOf(
        "unfortunately", "we regret", "regret to inform", "sorry to say",
        // Both tenses: real rejections say "will not be moving forward" as often
        // as "not moving forward".
        "not moving forward", "not be moving forward", "not move forward",
        "not taking your application further", "decided to close this position",
        "decided not to proceed", "not to proceed", "other candidates",
        "another candidate", "with candidates whose", "will not be progressing",
        "not be moving ahead", "no longer under consideration",
        "not been successful", "unsuccessful on this occasion",
        "unable to provide individual feedback",
        "wish you the best", "wish you all the best", "wish you every success",
        "best in your job search", "keep your details on file",
        "leider", "absage", "nicht weiter", "anderen kandidaten", "entschieden haben",
        "helaas", "afwijzing", "niet verder",
    )

    private val ACK = listOf(
        "we have received your application", "application received",
        "have received your documents", "review of your documents",
        "for applying", "thanks for applying", "thank you for your application",
        "thank you for signing up", "received your application",
        "application was submitted", "submitted successfully", "successfully submitted",
        "we are reviewing", "under review", "in the process of reviewing",
        "your application is being reviewed", "get in touch with you as soon as",
        "get back to you as soon as",
        "eingegangen", "bewerbung erhalten", "vielen dank für ihre bewerbung",
        "sollicitatie ontvangen", "bedankt voor je sollicitatie",
    )

    /**
     * Local-parts that mark bulk mail. Used only to decide whether *weak* evidence
     * (the bare word "interview" and friends) is enough to call something an
     * invitation — job-board alerts and career-coaching marketing are full of the
     * word, while a real reply comes from a person or an ATS.
     */
    private val BULK_SENDERS = listOf(
        "noreply", "no-reply", "donotreply", "newsletter", "jobalerts", "job-alerts",
        "alerts", "digest", "marketing", "updates", "notification",
    )

    private val JOB_WORDS = listOf(
        // "apply" also covers "applying"; "application"/"applied" don't contain it.
        "application", "applied", "apply", "candidate", "recruit", "role", "position", "hiring",
        "career", "interview", "assessment", "cv", "resume", "vacancy",
        "bewerbung", "stelle", "karriere", "personal",
        "sollicitatie", "vacature",
    )

    private fun countHits(text: String, patterns: List<String>): Int =
        patterns.count { it in text }

    /** Triage fallback: might this message be about an application? */
    fun looksJobRelated(header: MailHeader, applied: List<AppliedJob>): Boolean {
        if (header.domain in Matcher.ATS_DOMAINS) return true
        if (Matcher.ATS_DOMAINS.any { header.domain.endsWith(".$it") }) return true
        if (Matcher.match(header, "", applied) != null) return true
        val haystack = "${header.subject} ${header.fromName}".lowercase()
        if (JOB_WORDS.any { it in haystack }) return true
        if (countHits(haystack, ACTION_NEEDED) > 0) return true
        return countHits(haystack, STRONG_INVITE) > 0
    }

    /** True for mass-mail senders that aren't an ATS (job alerts, newsletters). */
    private fun isBulk(header: MailHeader): Boolean {
        if (header.domain in Matcher.ATS_DOMAINS) return false
        if (Matcher.ATS_DOMAINS.any { header.domain.endsWith(".$it") }) return false
        val local = header.fromAddress.substringBefore('@').lowercase()
        return BULK_SENDERS.any { it in local }
    }

    /** Status fallback. Never returns "unknown" — the caller flags it needs_review. */
    fun classify(header: MailHeader, body: String, applied: List<AppliedJob>): Verdict {
        val text = "${header.subject}\n$body".lowercase()
        val strong = countHits(text, STRONG_INVITE)
        // "If you are selected for an interview…" is an acknowledgement, and a
        // booking-page verification code is machine noise — in both the bare word
        // "interview"/"meeting" is not evidence of anything, so weak hits are void.
        val hedged = countHits(text, CONDITIONAL_INTERVIEW) > 0 ||
            countHits(text, TRANSACTIONAL) > 0
        val weak = if (hedged) 0 else countHits(text, WEAK_INVITE)
        val inviteScore = strong * 2 + weak
        val rejectScore = countHits(text, REJECT) * 2
        val action = countHits(text, ACTION_NEEDED)
        val job = Matcher.match(header, body, applied)

        // Weak evidence alone — the bare word "interview" — only counts when the
        // mail plausibly *is* a reply: matched to something applied to, or from a
        // person/ATS rather than a bulk sender. Otherwise every "ace your
        // interview" newsletter would read as an invitation.
        val weakCounts = job != null || !isBulk(header)

        val status = when {
            strong > 0 && inviteScore > rejectScore -> ReplyStatus.INVITED
            rejectScore >= 2 -> ReplyStatus.REJECTED
            // Ahead of the weak-invite and ack branches: "please complete 2
            // questions to finalize your application" is neither an invitation nor
            // a receipt, and reading it as either loses the deadline.
            action > 0 -> ReplyStatus.ACTION_NEEDED
            inviteScore >= 1 && weakCounts -> ReplyStatus.INVITED // err toward looking
            countHits(text, ACK) > 0 -> ReplyStatus.ACKNOWLEDGED
            else -> ReplyStatus.OTHER
        }
        return Verdict(
            status = status,
            job = job,
            // Heuristic verdicts are always provisional; the store flags them and
            // the next LLM-backed scan overwrites them with a real judgement.
            confidence = 40,
            classifiedBy = "heuristic",
        )
    }
}
