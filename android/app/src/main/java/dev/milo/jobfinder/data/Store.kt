package dev.milo.jobfinder.data

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/**
 * File-backed persistence, mirroring the Python app's job_decisions.json and
 * jobs.csv: swipe decisions keyed by normalized URL, plus the saved-jobs list.
 */
class Store(private val dir: File) {

    private val decisionsFile = File(dir, "job_decisions.json")
    private val jobsFile = File(dir, "jobs.json")

    // --- decisions -------------------------------------------------------------

    private val decisions: JSONObject by lazy {
        try {
            JSONObject(decisionsFile.readText())
        } catch (e: Exception) {
            JSONObject()
        }
    }

    fun isDecided(job: Job): Boolean = decisions.has(job.decisionKey)

    /** Record a swipe; returns the decision record (for the sync push). */
    fun decide(job: Job, liked: Boolean): JSONObject {
        val record = JSONObject()
            .put("decision", if (liked) "yes" else "no")
            .put("title", job.title)
            .put("company", job.company)
            .put("ts", LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm")))
        decisions.put(job.decisionKey, record)
        persistDecisions()
        return record
    }

    fun undecide(job: Job) {
        decisions.remove(job.decisionKey)
        persistDecisions()
    }

    private fun persistDecisions() {
        try {
            decisionsFile.writeText(decisions.toString(2))
        } catch (e: Exception) { /* best-effort */ }
    }

    // --- saved jobs --------------------------------------------------------------

    fun loadJobs(): List<Job> {
        return try {
            val arr = JSONArray(jobsFile.readText())
            (0 until arr.length()).map { Job.fromJson(arr.getJSONObject(it)) }
        } catch (e: Exception) {
            emptyList()
        }
    }

    /** Append new jobs, deduplicating by normalized URL. Returns the added jobs. */
    fun saveJobs(jobs: List<Job>): List<Job> {
        val existing = loadJobs().toMutableList()
        val known = existing.map { it.normUrl }.toMutableSet()
        val added = mutableListOf<Job>()
        for (job in jobs) {
            val u = job.normUrl
            if (u.isNotEmpty() && u !in known) {
                existing.add(job)
                known.add(u)
                added.add(job)
            }
        }
        if (added.isNotEmpty()) {
            val arr = JSONArray()
            existing.forEach { arr.put(it.toJson()) }
            try {
                jobsFile.writeText(arr.toString())
            } catch (e: Exception) { /* best-effort */ }
        }
        return added
    }

    fun undecidedSavedJobs(): List<Job> = loadJobs().filter { !isDecided(it) }

    // --- cross-device sync ---------------------------------------------------------

    /**
     * Two-way merge with the sync DB: pull remote decisions (later ts wins) and
     * jobs, then push back anything only this device has. Returns a short
     * human-readable summary, or null when sync is off/unreachable.
     */
    fun syncWithRemote(): String? {
        if (!Sync.enabled) return null
        val remoteDecisions = Sync.pullDecisions()
        val remoteJobs = Sync.pullJobs()
        if (remoteDecisions.isEmpty() && remoteJobs.isEmpty()) {
            // Could be an empty DB — still push local state up so it seeds.
            Sync.pushDecisions(decisions)
            Sync.pushJobs(loadJobs())
            return "sync: seeded remote"
        }
        var mergedDecisions = 0
        for ((localKey, record) in remoteDecisions) {
            val mine = decisions.optJSONObject(localKey)
            if (mine == null || record.optString("ts") > mine.optString("ts")) {
                decisions.put(localKey, record)
                mergedDecisions++
            }
        }
        if (mergedDecisions > 0) persistDecisions()
        val newJobs = saveJobs(remoteJobs)
        // Push back what only this device knows.
        Sync.pushDecisions(decisions)
        val remoteKeys = remoteJobs.map { Sync.jobKey(it) }.toSet()
        Sync.pushJobs(loadJobs().filter { Sync.jobKey(it) !in remoteKeys })
        return "sync: +${newJobs.size} jobs, +$mergedDecisions decisions"
    }
}
