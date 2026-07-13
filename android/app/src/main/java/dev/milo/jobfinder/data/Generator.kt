package dev.milo.jobfinder.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * Cover letter / application answer / salary generation — ported from
 * cover_letter.py. The resume (pre-rendered plain text) and the cover-letter
 * style guide ship as app assets.
 */
class Generator(context: Context, private val descFetcher: DescFetcher) {

    private val resume: String = context.assets.open("resume.txt").bufferedReader().readText()
    private val styleGuide: String =
        context.assets.open("styleguide.md").bufferedReader().readText()
    private val candidateName = "Milo de Vries"

    // --- system prompts (verbatim from cover_letter.py) ---------------------------

    private fun coverLetterSystem(): String = """You are writing a cover letter as $candidateName. Write ONLY the cover letter text.
No preamble, no commentary, no "Here is your cover letter" — just the letter itself.

The example cover letters in the style guide are the authoritative voice to match.
Study them carefully before writing. That register — direct, casual-but-sharp, personal, real — is the target.

STYLE GUIDE AND EXAMPLES:
$styleGuide

RESUME:
$resume

DO NOT sound like AI or LinkedIn. Never use:
- "I am excited/thrilled/passionate to apply..."
- "I am confident that I would be a great asset..."
- "My experience has equipped me with..."
- "I am eager to contribute to your team..."
- Any bullet points or list formatting in the letter body
- Em-dashes (—)
- Buzzwords: "leverage", "synergies", "impact-driven", "stakeholder", "fast-paced environment", "team player"
- The dramatic backwards setup: "[description of company/product] — that's exactly the kind of X I want to be doing/building."
- "not just X, but Y" / "not only... but also" constructions
- Rule-of-three lists ("clear, focused, and effective")
- Hedged openers: "I believe that...", "I feel that..."
- "resonates", "aligns with my values", "genuinely excites me"
- Ending every paragraph on a tidy punchline

SOUND LIKE A PERSON:
- Vary the rhythm. A long winding sentence, then a short one. Fragments are fine.
- AI writes every sentence at the same medium length with neat parallel structure. Don't.
- Include one or two touches of real character: a dry aside, a parenthetical remark, a concrete opinion about the company's product or problem.
- Starting a sentence with "And", "But" or "Which" is fine. So is a mild colloquialism.
- Small imperfections are human: a slightly informal construction, a thought that runs on a bit. But NEVER spelling errors or typos.
- Don't polish every transition; people jump between thoughts.
- It should read like it was typed in one sitting by someone sharp who means it, not assembled by committee.

RULES:
- 150-220 words. Short paragraphs.
- Only draw from the resume. Never fabricate experience.
- End simply: "I look forward to expanding on this in an interview."
"""

    private fun questionSystem(): String = """You are answering an application form question as $candidateName.
Write ONLY the answer. No preamble, no commentary.

The cover letter examples in the style guide define the voice — direct, casual-but-sharp, real.
Apply that same register to application answers: plain language, precise verbs, no performance.

STYLE GUIDE AND EXAMPLES:
$styleGuide

RESUME:
$resume

DO NOT sound like AI or LinkedIn. Never use:
- "I am passionate about..."
- "I thrive in fast-paced environments..."
- "I am a strong communicator/collaborator..."
- "This opportunity excites me because..."
- Buzzwords: "leverage", "synergies", "impact-driven", "stakeholder", "team player"
- "not just X, but Y" / "not only... but also" constructions
- Rule-of-three lists ("clear, focused, and effective")
- Hedged openers: "I believe that...", "I feel that..."
- "resonates", "aligns with my values", "genuinely excites me"

SOUND LIKE A PERSON:
- Vary the rhythm. A long winding sentence, then a short one. Fragments are fine.
- AI writes every sentence at the same medium length with neat parallel structure. Don't.
- A dry aside, a parenthetical remark, or a concrete opinion is welcome where it fits.
- Starting a sentence with "And", "But" or "Which" is fine. So is a mild colloquialism.
- Small imperfections are human: a slightly informal construction, a thought that runs on a bit. But NEVER spelling errors or typos.
- It should read like someone sharp answering honestly, not performing for a form.

RULES:
- 50-200 words depending on the question. Shorter is usually better.
- Only draw from the resume. Never fabricate experience.
- Answer the actual question directly — don't warm up to it.
"""

    // Appended to the cover-letter system prompt for "pearl" supermatches: a rough,
    // riskier single-pass draft that Milo will rewrite by hand anyway.
    private val drunkAddendum = """

PEARL MODE — this company is a dream match, and this draft is deliberately a rough
one: Milo will rewrite it by hand. So loosen up and swing harder than usual:
- Take creative risks. An unexpected opening, a weird-but-apt metaphor, an
  opinionated hot take on their product or market. If a line makes you hesitate
  "is this too much?" — keep it.
- Be funnier and more personal than feels safe. Overshoot the voice rather than
  undershoot it.
- A half-finished thought or a missing transition is fine. Interesting beats
  polished. Do NOT sand the edges afterwards.
- The banned-phrases list still applies, and the facts must still come straight
  from the resume. Wild delivery, true content.
- 150-260 words."""

    private val reviseInstruction =
        """Now re-read your draft as a skeptical editor who can smell AI writing a mile off.
Hunt for: uniform sentence rhythm, symmetric constructions, tidy punchline endings,
rule-of-three lists, generic claims that could appear in anyone's application,
anything performed rather than said, and any banned pattern that slipped through.
Rewrite it, fixing what you find and keeping what already sounds human. Keep the
same facts and roughly the same length. Output ONLY the final text — no commentary,
no explanation of what you changed."""

    private val salarySystem = "You are a compensation expert with deep knowledge of tech and " +
        "climate-tech salaries across Europe and globally. Given a job posting, provide a " +
        "concise salary estimate. Structure your answer as:\n" +
        "- Estimated range (annual, gross) in the relevant local currency\n" +
        "- Key factors influencing the estimate (location, seniority, company type)\n" +
        "- A brief note on equity/bonus if typical for this type of role\n" +
        "Be specific and data-driven. No fluff. If location is ambiguous, state your assumption."

    // --- job context ---------------------------------------------------------------

    fun buildJobContext(job: Job?, fullDescription: String = ""): String {
        if (job == null && fullDescription.isBlank()) return ""
        val parts = mutableListOf<String>()
        if (job != null) {
            parts.add("Title: ${job.title.ifBlank { "Unknown" }}")
            parts.add("Company: ${job.company}")
            if (job.location.isNotBlank()) parts.add("Location: ${job.location}")
            if (job.salary.isNotBlank() && job.salary != "Not listed") parts.add("Salary: ${job.salary}")
            if (job.wfhPolicy.isNotBlank()) parts.add("WFH Policy: ${job.wfhPolicy}")
            if (job.url.isNotBlank()) parts.add("URL: ${job.url}")
            if (job.snippet.isNotBlank()) parts.add("Description snippet: ${job.snippet}")
        }
        if (fullDescription.isNotBlank()) parts.add("\nFull job description:\n$fullDescription")
        return parts.joinToString("\n")
    }

    fun fetchDescription(url: String): String = descFetcher.fetch(url)

    /** "COMPANY | ROLE" extraction via Haiku, for jobs that only exist as a URL. */
    fun extractJobMeta(text: String): Pair<String, String> {
        if (text.isBlank()) return "Unknown" to "Role"
        return try {
            val reply = Anthropic.complete(
                model = Anthropic.FAST_MODEL,
                maxTokens = 60,
                user = "Extract the hiring company name and job title from the text below. " +
                    "Reply with exactly this format (no extra words): COMPANY | ROLE\n\n" +
                    text.take(3000),
            )
            val parts = reply.trim().split("|")
            if (parts.size >= 2) parts[0].trim() to parts[1].trim() else "Unknown" to "Role"
        } catch (e: Exception) {
            "Unknown" to "Role"
        }
    }

    // --- generation (all streaming) ---------------------------------------------------

    fun streamCoverLetter(
        jobContext: String,
        extraContext: String,
        drunk: Boolean = false,
        shouldStop: () -> Boolean,
        onDelta: (String) -> Unit,
    ): String {
        var user = "Write a cover letter for this position:\n\n$jobContext"
        if (extraContext.isNotBlank()) user += "\n\nAdditional context: $extraContext"
        if (drunk) {
            // Pearl mode: one rough, riskier pass — no self-critique polish.
            return Anthropic.stream(
                Anthropic.GENERATION_MODEL, 1024,
                coverLetterSystem() + drunkAddendum, user, shouldStop, onDelta,
            )
        }
        return streamWithRevision(coverLetterSystem(), user, shouldStop, onDelta)
    }

    fun streamAnswer(
        question: String,
        jobContext: String,
        shouldStop: () -> Boolean,
        onDelta: (String) -> Unit,
    ): String {
        var user = "Answer this application question:\n\n$question"
        if (jobContext.isNotBlank()) user += "\n\nJob context:\n$jobContext"
        return streamWithRevision(questionSystem(), user, shouldStop, onDelta)
    }

    /**
     * Two-pass generation (mirrors cover_letter.py): a silent draft, then a
     * self-critique pass that hunts AI tells and streams the rewrite. If the
     * revision pass fails, the draft is emitted as-is.
     */
    private fun streamWithRevision(
        system: String,
        user: String,
        shouldStop: () -> Boolean,
        onDelta: (String) -> Unit,
    ): String {
        val draft = Anthropic.complete(Anthropic.GENERATION_MODEL, 1024, system, user)
        if (shouldStop()) return draft
        val messages = JSONArray()
            .put(JSONObject().put("role", "user").put("content", user))
            .put(JSONObject().put("role", "assistant").put("content", draft))
            .put(JSONObject().put("role", "user").put("content", reviseInstruction))
        return try {
            Anthropic.stream(Anthropic.GENERATION_MODEL, 1024, system, messages, shouldStop, onDelta)
        } catch (e: Exception) {
            onDelta(draft)
            draft
        }
    }

    fun streamSalary(
        jobContext: String,
        shouldStop: () -> Boolean,
        onDelta: (String) -> Unit,
    ): String = Anthropic.stream(
        Anthropic.FAST_MODEL, 512, salarySystem,
        "Estimate the salary for this role:\n\n$jobContext", shouldStop, onDelta
    )

    /** Company research: no DuckDuckGo on-device, so ask Haiku what it knows. */
    fun companyInfo(company: String): String {
        if (company.isBlank() || company == "Unknown") return "(no company name)"
        return try {
            Anthropic.complete(
                model = Anthropic.FAST_MODEL,
                maxTokens = 400,
                user = "In a few short lines: what does the company \"$company\" do, " +
                    "what is its climate/sustainability angle (if any), and what is known " +
                    "about its remote-work policy? If you don't know the company, say so " +
                    "plainly. Note: your knowledge may be outdated.",
            )
        } catch (e: Exception) {
            "(lookup failed: ${e.message})"
        }
    }
}
