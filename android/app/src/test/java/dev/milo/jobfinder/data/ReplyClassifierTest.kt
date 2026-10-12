package dev.milo.jobfinder.data

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Recall tests for the no-LLM fallback, run against labelled samples from a real
 * inbox (anonymised, in mail_fixtures.json).
 *
 * The asserted bar is asymmetric on purpose: **every** invitation must be caught,
 * while false positives are only reported, not failed. A missed interview
 * invitation is exactly the failure this feature exists to prevent; an extra row
 * in the replies list costs a glance.
 */
class ReplyClassifierTest {

    private data class Sample(val header: MailHeader, val body: String, val expect: String)

    private val fixtures: JSONObject by lazy {
        val stream = javaClass.classLoader!!.getResourceAsStream("mail_fixtures.json")
        assertNotNull("mail_fixtures.json missing from test resources", stream)
        JSONObject(stream!!.bufferedReader().readText())
    }

    private val applied: List<AppliedJob> by lazy {
        val arr = fixtures.getJSONArray("applied")
        (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            AppliedJob(o.getString("key"), o.getString("company"), o.getString("title"))
        }
    }

    private val samples: List<Sample> by lazy {
        val arr = fixtures.getJSONArray("messages")
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Sample(header(o.getString("from"), o.getString("subject"), i), o.getString("body"), o.getString("expect"))
        }
    }

    private fun header(from: String, subject: String, i: Int): MailHeader {
        val m = Regex("^(.*?)\\s*<([^>]+)>$").find(from.trim())
        return MailHeader(
            uid = i.toLong(),
            messageId = "<fixture-$i@test>",
            fromName = m?.groupValues?.get(1)?.trim('"', ' ').orEmpty(),
            fromAddress = m?.groupValues?.get(2) ?: from.trim(),
            subject = subject,
            receivedAt = 0L,
        )
    }

    @Test
    fun `every invitation survives triage`() {
        val missed = samples
            .filter { it.expect == ReplyStatus.INVITED }
            .filterNot { Heuristics.looksJobRelated(it.header, applied) }
            .map { it.header.subject }
        assertEquals("triage dropped invitation(s): $missed", emptyList<String>(), missed)
    }

    @Test
    fun `every invitation is classified as invited`() {
        val invites = samples.filter { it.expect == ReplyStatus.INVITED }
        assertTrue("fixture set has no invitations to test", invites.isNotEmpty())
        val missed = invites
            .map { it to Heuristics.classify(it.header, it.body, applied) }
            .filter { (_, verdict) -> verdict.status != ReplyStatus.INVITED }
            .map { (sample, verdict) -> "${sample.header.subject} -> ${verdict.status}" }
        assertEquals("recall on invitations must be 1.00; missed: $missed", emptyList<String>(), missed)
    }

    @Test
    fun `every unfinished application survives triage and is flagged`() {
        // Same asymmetry as invitations: an application that dies because two
        // screening questions went unanswered is the failure mode, so recall is
        // asserted. These arrive from screening vendors whose links literally say
        // /assessment/invite — hence the separate status rather than "invited".
        val todos = samples.filter { it.expect == ReplyStatus.ACTION_NEEDED }
        assertTrue("fixture set has no action_needed samples", todos.isNotEmpty())

        val dropped = todos
            .filterNot { Heuristics.looksJobRelated(it.header, applied) }
            .map { it.header.subject }
        assertEquals("triage dropped: $dropped", emptyList<String>(), dropped)

        val missed = todos
            .map { it to Heuristics.classify(it.header, it.body, applied) }
            .filter { (_, v) -> v.status != ReplyStatus.ACTION_NEEDED }
            .map { (s, v) -> "${s.header.subject} -> ${v.status}" }
        assertEquals("recall on action_needed must be 1.00; missed: $missed", emptyList<String>(), missed)
    }

    @Test
    fun `an acknowledgement from the same ATS is not read as an unfinished step`() {
        // "You have successfully submitted your application… should your profile
        // match, we will be in touch to schedule the next steps" — nothing to do,
        // and the next steps are conditional. Neither todo nor invite.
        val ack = samples.first { it.header.subject.contains("Application review in process") }
        assertEquals(
            ReplyStatus.ACKNOWLEDGED,
            Heuristics.classify(ack.header, ack.body, applied).status,
        )
    }

    @Test
    fun `rejections are not mistaken for invitations`() {
        // Not a hard recall bar — a rejection read as an invite is a wasted glance,
        // not a missed opportunity. Reported so regressions are visible.
        val rejections = samples.filter { it.expect == ReplyStatus.REJECTED }
        val wrong = rejections.count {
            Heuristics.classify(it.header, it.body, applied).status == ReplyStatus.INVITED
        }
        println("rejections read as invites: $wrong/${rejections.size}")
        assertTrue(
            "every rejection was read as an invitation — the weighting is broken",
            rejections.isEmpty() || wrong < rejections.size,
        )
    }

    @Test
    fun `obvious noise is filtered and precision is reported`() {
        val noise = samples.filter { it.expect == "noise" }
        val leaked = noise.filter { Heuristics.looksJobRelated(it.header, applied) }
        println("noise reaching the classifier: ${leaked.size}/${noise.size} ${leaked.map { it.header.subject }}")

        val flagged = samples.count {
            Heuristics.classify(it.header, it.body, applied).status == ReplyStatus.INVITED
        }
        val real = samples.count { it.expect == ReplyStatus.INVITED }
        println("invited: $real real, $flagged flagged (precision ${"%.2f".format(real.toDouble() / flagged)})")
    }

    @Test
    fun `status accuracy is reported per label`() {
        // Not asserted beyond invitations — getting a rejection filed as "other"
        // is cosmetic. Printed so a pattern change that degrades it is visible.
        val byLabel = samples.filter { it.expect != "noise" }.groupBy { it.expect }
        for ((expected, group) in byLabel) {
            val got = group.map { Heuristics.classify(it.header, it.body, applied).status }
            val right = got.count { it == expected }
            println("$expected: $right/${group.size} correct  (got: ${got.distinct()})")
        }
    }

    @Test
    fun `replies are matched to the job they are about`() {
        // Sender domain carries the company: m.delaunay@aurex.energy.
        val invite = samples.first { it.header.subject.startsWith("Video interview") }
        assertEquals(
            "aurex energy|lead product manager",
            Matcher.match(invite.header, invite.body, applied)?.key,
        )
    }

    @Test
    fun `an ATS sender is matched by the company in the body, not the domain`() {
        // notifications@recruiteemail.com says nothing about who is hiring — the
        // company name only appears in the subject and body.
        val confirmation = samples.first { it.header.subject.startsWith("Confirmation:") }
        assertEquals(
            "aurex energy|lead product manager",
            Matcher.match(confirmation.header, confirmation.body, applied)?.key,
        )
    }

    @Test
    fun `company keys drop legal suffixes like the python side`() {
        // Ground truth is connections.py company_key — these are its actual outputs,
        // so a job grouped on the laptop groups the same way here.
        assertEquals("enpal", Matcher.companyKey("Enpal GmbH"))
        assertEquals("1komma5", Matcher.companyKey("1Komma5° GmbH"))
        assertEquals("acme and sons", Matcher.companyKey("The Acme & Sons Group"))
        // Dotted suffixes survive normalization on both sides ("b v" are not words
        // company_key strips) — which is why Matcher falls back to the lead token.
        assertEquals("overstory b v", Matcher.companyKey("Overstory B.V."))
    }

    @Test
    fun `a dotted legal suffix still matches the company domain`() {
        val applied = listOf(AppliedJob("overstory|product lead", "Overstory B.V.", "Product Lead"))
        val mail = header("Careers <talent@overstory.com>", "About your application", 99)
        assertEquals("overstory|product lead", Matcher.match(mail, "", applied)?.key)
    }

    @Test
    fun `spent api keys pause the classifier but timeouts do not`() {
        assertTrue(LlmBudget.isTerminal(RuntimeException("API 400: Your credit balance is too low")))
        assertTrue(LlmBudget.isTerminal(RuntimeException("API 401: invalid x-api-key")))
        assertTrue(LlmBudget.isTerminal(RuntimeException("API 429: rate limit exceeded")))
        assertTrue(!LlmBudget.isTerminal(RuntimeException("API 500: internal server error")))
        assertTrue(!LlmBudget.isTerminal(java.net.SocketTimeoutException("timeout")))
    }
}
