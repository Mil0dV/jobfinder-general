package dev.milo.jobfinder.data

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * Exercises the real triage + classify prompts against the same labelled samples
 * as [ReplyClassifierTest]. Opt-in — it spends API credit and needs the network:
 *
 *     JOBFINDER_LIVE_TEST=1 ./gradlew :app:testDebugUnitTest --tests '*LiveClassifierTest*'
 *
 * Without the flag every test here is skipped, so the normal build stays offline
 * and free. The bar is the same as the offline suite: every invitation must
 * survive triage and be classified `invited`.
 */
class LiveClassifierTest {

    private val enabled = System.getenv("JOBFINDER_LIVE_TEST") == "1"

    private data class Sample(val header: MailHeader, val body: String, val expect: String)

    private val fixtures: JSONObject by lazy {
        JSONObject(
            javaClass.classLoader!!.getResourceAsStream("mail_fixtures.json")!!
                .bufferedReader().readText()
        )
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
            val from = o.getString("from")
            val m = Regex("^(.*?)\\s*<([^>]+)>$").find(from.trim())
            Sample(
                MailHeader(
                    uid = i.toLong(),
                    messageId = "<fixture-$i@test>",
                    fromName = m?.groupValues?.get(1)?.trim('"', ' ').orEmpty(),
                    fromAddress = m?.groupValues?.get(2) ?: from.trim(),
                    subject = o.getString("subject"),
                    receivedAt = 0L,
                ),
                o.getString("body"),
                o.getString("expect"),
            )
        }
    }

    @Test
    fun `triage keeps every invitation and drops most noise`() {
        assumeTrue("set JOBFINDER_LIVE_TEST=1 to run", enabled)
        assumeTrue("Keys.ANTHROPIC_API_KEY is blank", Keys.ANTHROPIC_API_KEY.isNotBlank())

        val headers = samples.map { it.header }
        val kept = ReplyClassifier.triage(headers, applied).map { it.uid }.toSet()

        val mustKeep = setOf(ReplyStatus.INVITED, ReplyStatus.ACTION_NEEDED)
        val missed = samples.filter { it.expect in mustKeep && it.header.uid !in kept }
        assertEquals(
            "triage dropped a message that needs Milo: ${missed.map { it.header.subject }}",
            emptyList<String>(), missed.map { it.header.subject },
        )
        val noise = samples.filter { it.expect == "noise" }
        val leaked = noise.count { it.header.uid in kept }
        println("live triage: kept ${kept.size}/${headers.size}, noise leaked $leaked/${noise.size}")
    }

    @Test
    fun `classify labels every invitation as invited`() {
        assumeTrue("set JOBFINDER_LIVE_TEST=1 to run", enabled)
        assumeTrue("Keys.ANTHROPIC_API_KEY is blank", Keys.ANTHROPIC_API_KEY.isNotBlank())

        val wrong = mutableListOf<String>()
        var correct = 0
        val judged = samples.filter { it.expect != "noise" }
        // Both of these notify, so for the asserted bar they're interchangeable:
        // what must never happen is one of them landing on ack/rejected/other.
        val notifying = setOf(ReplyStatus.INVITED, ReplyStatus.ACTION_NEEDED)
        for (s in judged) {
            val verdict = ReplyClassifier.classify(s.header, s.body, applied)
            if (verdict.status == s.expect) correct++
            if (s.expect in notifying && verdict.status !in notifying) {
                wrong.add("${s.header.subject} -> ${verdict.status}")
            }
        }
        println("live classify: $correct/${judged.size} exact")
        assertEquals("recall on things needing Milo must be 1.00; missed: $wrong", emptyList<String>(), wrong)
        assertTrue(correct > 0)
    }
}
