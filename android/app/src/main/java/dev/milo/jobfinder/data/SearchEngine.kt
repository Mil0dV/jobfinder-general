package dev.milo.jobfinder.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/** Search progress, mirrored from the TUI's _progress callback. */
data class Progress(
    val phase: String, // sources | verify | descriptions | validate | done
    val done: Int,
    val total: Int,
    val detail: String,
)

/**
 * The search pipeline, ported from agent.py's search_jobs():
 * parallel sources → dedupe → age cutoff → liveness check → description fetch
 * → Claude validation → threshold + rank.
 */
class SearchEngine(private val descFetcher: DescFetcher) {

    companion object {
        const val MAX_AGE_DAYS = 90L
        const val VERIFY_AGE_DAYS = 30L
        const val MIN_FIT = 3
        const val MIN_CLIMATE = 2
        const val MAX_RESULTS = 50

        // Sources that only list climate jobs — climate relevance is inherent.
        val CLIMATE_BOARD_SOURCES = setOf(
            "climatebase", "climatetechlist", "greenhouse", "lever",
            "jobsincarbon", "remoteimpact",
        )
    }

    suspend fun search(query: String, onProgress: (Progress) -> Unit): List<Job> =
        withContext(Dispatchers.IO) {
            // 1. All sources in parallel.
            val allJobs = mutableListOf<Job>()
            coroutineScope {
                var done = 0
                val jobsBySource = Sources.ALL.map { (name, fn) ->
                    async {
                        val result = try {
                            fn(query)
                        } catch (e: Exception) {
                            null
                        }
                        synchronized(allJobs) {
                            done++
                            if (result != null) allJobs.addAll(result)
                            onProgress(
                                Progress(
                                    "sources", done, Sources.ALL.size,
                                    "$name:${result?.size?.toString() ?: "error"}"
                                )
                            )
                        }
                    }
                }
                jobsBySource.awaitAll()
            }

            // 2. Dedupe by normalized URL, then by (company, title); drop stale postings.
            val seenUrls = mutableSetOf<String>()
            val seenKeys = mutableSetOf<Pair<String, String>>()
            val unique = mutableListOf<Job>()
            for (job in allJobs) {
                val url = job.normUrl
                val key = job.company.lowercase() to job.title.lowercase()
                if ((url.isNotEmpty() && url in seenUrls) || key in seenKeys) continue
                if (url.isNotEmpty()) seenUrls.add(url)
                seenKeys.add(key)
                val age = job.ageDays()
                if (age != null && age > MAX_AGE_DAYS) continue
                unique.add(job)
            }

            // 3. Liveness check for older postings.
            val alive = verifyOnline(unique, onProgress)

            // 4. Full descriptions (validation scores real text; cards preview instantly).
            fetchDescriptions(alive, onProgress)

            // 5. Claude validation in parallel batches.
            val validated = validate(alive, query, onProgress)

            // Pearls (supermatches) first, then most recent, fit/climate as tiebreak.
            val ranked = validated.sortedWith(
                compareByDescending<Job> { it.isPearl }
                    .thenByDescending { it.postedDate }
                    .thenByDescending { it.fitScore }
                    .thenByDescending { it.climateScore }
            ).take(MAX_RESULTS)
            onProgress(Progress("done", 1, 1, "${ranked.size} jobs"))
            ranked
        }

    // --- liveness ------------------------------------------------------------------

    private suspend fun verifyOnline(jobs: List<Job>, onProgress: (Progress) -> Unit): List<Job> {
        val toCheck = jobs.filter { job ->
            job.url.startsWith("http") && (job.ageDays() ?: 0) > VERIFY_AGE_DAYS
        }
        if (toCheck.isEmpty()) return jobs

        val offline = java.util.Collections.synchronizedSet(mutableSetOf<Job>())
        coroutineScope {
            val sem = Semaphore(10)
            var done = 0
            toCheck.map { job ->
                async(Dispatchers.IO) {
                    sem.withPermit {
                        if (!isOnline(job)) offline.add(job)
                        synchronized(offline) {
                            done++
                            onProgress(Progress("verify", done, toCheck.size, job.company))
                        }
                    }
                }
            }.awaitAll()
        }
        return jobs.filter { it !in offline }
    }

    /**
     * A posting counts as offline ONLY on a definitive 404/410 or an ATS
     * soft-404 (redirect to the board root with ?error=true / job-id segment
     * gone). Bot walls, server errors, and timeouts keep the job.
     */
    private fun isOnline(job: Job): Boolean {
        return try {
            Http.request(job.url).use { resp ->
                if (resp.code in setOf(404, 410)) return false
                if (resp.priorResponse != null) { // we were redirected
                    val finalUrl = resp.request.url.toString()
                    if ("error=true" in finalUrl) return false
                    val segment = job.url.substringBefore("?").trimEnd('/').substringAfterLast("/")
                    if (segment.length >= 6 && segment.any { it.isDigit() } && segment !in finalUrl) {
                        return false
                    }
                }
                true
            }
        } catch (e: Exception) {
            true // network trouble is not evidence the posting is gone
        }
    }

    // --- descriptions -----------------------------------------------------------------

    private suspend fun fetchDescriptions(jobs: List<Job>, onProgress: (Progress) -> Unit) {
        val toFetch = jobs.filter { it.url.startsWith("http") && it.desc == null }
        if (toFetch.isEmpty()) return
        coroutineScope {
            val sem = Semaphore(12)
            var done = 0
            toFetch.map { job ->
                async(Dispatchers.IO) {
                    sem.withPermit {
                        val text = try {
                            descFetcher.fetch(job.url)
                        } catch (e: Exception) {
                            ""
                        }
                        if (text.isNotBlank()) job.desc = text
                        synchronized(toFetch) {
                            done++
                            onProgress(Progress("descriptions", done, toFetch.size, job.company))
                        }
                    }
                }
            }.awaitAll()
        }
    }

    // --- validation ---------------------------------------------------------------------

    private val validationSchema = JSONObject(
        """
        {
          "type": "object",
          "properties": {
            "evaluations": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "id": {"type": "integer"},
                  "fit": {"type": "integer"},
                  "startup": {"type": "integer"},
                  "climate": {"type": "integer"},
                  "reason": {"type": "string"}
                },
                "required": ["id", "fit", "startup", "climate", "reason"],
                "additionalProperties": false
              }
            }
          },
          "required": ["evaluations"],
          "additionalProperties": false
        }
        """
    )

    private suspend fun validate(
        jobs: List<Job>,
        query: String,
        onProgress: (Progress) -> Unit,
    ): List<Job> {
        if (jobs.isEmpty()) return emptyList()
        val batches = jobs.chunked(15)
        val kept = java.util.Collections.synchronizedList(mutableListOf<Job>())
        coroutineScope {
            val sem = Semaphore(6)
            var done = 0
            batches.map { batch ->
                async(Dispatchers.IO) {
                    sem.withPermit {
                        scoreBatch(batch, query)
                        synchronized(kept) {
                            kept.addAll(batch.filter { it.fitScore >= MIN_FIT && it.climateScore >= MIN_CLIMATE })
                            done++
                            onProgress(Progress("validate", done, batches.size, "${kept.size} kept"))
                        }
                    }
                }
            }.awaitAll()
        }
        return kept.toList()
    }

    /** Score one batch in place; falls back to source-based priors when the API fails. */
    private fun scoreBatch(batch: List<Job>, query: String) {
        val roleTarget = if (query == Profile.PROFILE_QUERY) "the candidate profile below"
        else "the search query \"$query\" and the candidate profile below"
        val jobsText = batch.mapIndexed { i, j ->
            "ID $i: ${j.title} at ${j.company} (${j.location.ifBlank { "?" }}) " +
                "- ${(j.desc ?: j.snippet).take(1000)}"
        }.joinToString("\n")

        val prompt = """Evaluate these job postings for a specific candidate.

CANDIDATE PROFILE:
${Profile.CANDIDATE_PROFILE}

JOBS:
$jobsText

Respond with JSON: an "evaluations" array with one object per job ID, each with:
- id: the job ID from the list above.
- fit: 0-5. How well the ROLE matches $roleTarget. Score the role itself:
  hands-on technical focus, product/vision ownership, and team leadership.
  5 = exact target role at right seniority (e.g. Head of Product, CTO, Founding Engineer,
      Technical/Platform PM, Director of Engineering, EiR) with real ownership
  3 = adjacent role or seniority slightly off (e.g. plain Product Manager, Engineering
      Manager), or ownership unclear
  0-1 = wrong role family (sales, marketing, hardware/mechanical, junior roles, internships)
- startup: 0-5. How startup-like (non-bureaucratic) the COMPANY feels, judged from the
  posting's tone, company size/stage, and process signals.
  5 = scrappy startup/scale-up: small team, founders close, fast decisions, ship-first
  3 = grown-up scale-up: some process and structure, still moves quickly
  0-1 = big corporate / government / consultancy: layers, committees, enterprise
        process, compliance-speak, "alignment with stakeholders"
- climate: 0-5. Climate relevance of the COMPANY/mission.
  5 = core climate mission (carbon removal, renewables, grid, climate SaaS)
  3 = meaningful sustainability angle
  0-1 = no climate connection, or fossil/heavy-polluter
- reason: one short sentence.

Example:
{"evaluations": [{"id": 0, "fit": 5, "startup": 4, "climate": 4, "reason": "Head of Product at a carbon accounting startup"}]}"""

        val resultsById: Map<Int, JSONObject>? = try {
            val text = Anthropic.complete(
                model = Anthropic.FAST_MODEL,
                maxTokens = 2000,
                user = prompt,
                outputSchema = validationSchema,
            )
            val evals = JSONObject(text).optJSONArray("evaluations") ?: JSONArray()
            (0 until evals.length()).associate {
                val ev = evals.getJSONObject(it)
                ev.optInt("id", -1) to ev
            }
        } catch (e: Exception) {
            null
        }

        batch.forEachIndexed { i, job ->
            if (resultsById == null) {
                // No LLM: titles already passed the profile regex, assume moderate
                // fit; trust climate-only boards for climate relevance.
                job.fitScore = 3
                job.startupScore = 3
                job.climateScore = if (job.source in CLIMATE_BOARD_SOURCES) 4 else 0
                job.reason = "unvalidated (no LLM available)"
            } else {
                val res = resultsById[i]
                job.fitScore = res?.optInt("fit", 3) ?: 3
                job.startupScore = res?.optInt("startup", 3) ?: 3
                job.climateScore = res?.optInt("climate", 0) ?: 0
                job.reason = res?.optString("reason") ?: ""
                if (job.climateScore == 0 && job.source in CLIMATE_BOARD_SOURCES) {
                    job.climateScore = 3 // climate board listing outranks a missing judgment
                }
            }
        }
    }
}
