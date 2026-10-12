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
class Store private constructor(private val dir: File) {

    companion object {
        @Volatile
        private var instance: Store? = null

        /**
         * The one instance for this process. Every store below keeps its JSON in
         * memory and writes the *whole* file back, so two instances silently
         * clobber each other: the background inbox scan (which builds its own
         * Store) would save a record, notify about it, and then the long-lived
         * Store behind the UI — still holding the pre-scan snapshot — would show
         * a stale list and overwrite the file on its next save. Sharing one
         * instance is what makes a notification and the replies list agree.
         */
        fun of(dir: File): Store = instance ?: synchronized(this) {
            instance ?: Store(dir).also { instance = it }
        }
    }

    private val decisionsFile = File(dir, "job_decisions.json")
    private val jobsFile = File(dir, "jobs.json")

    /** Classified replies to applications (written by the inbox scan). */
    val replies = RepliesStore(dir)

    // --- decisions -------------------------------------------------------------

    private val decisions: JSONObject by lazy {
        val obj = try {
            JSONObject(decisionsFile.readText())
        } catch (e: Exception) {
            JSONObject()
        }
        if (migrateDecisionKeys(obj)) {
            try {
                decisionsFile.writeText(obj.toString(2))
            } catch (e: Exception) { /* best-effort */ }
        }
        obj
    }

    /**
     * The canonical decision key for a record: company|title (lowercased), or the
     * given fallback (the record's own key) when there's no real company+title.
     * Matches Job.decisionKey / sync.norm_key, so a decision made under any old or
     * raw-cased key resolves to the same identity everywhere.
     */
    private fun canonicalKey(fallback: String, rec: JSONObject): String {
        val c = rec.optString("company").trim().lowercase()
        val t = rec.optString("title").trim().lowercase()
        return if (c.isNotEmpty() && c != "unknown" && t.isNotEmpty()) "$c|$t" else fallback
    }

    /**
     * Idempotent re-key of stored decisions from any old identity (URL-first, or
     * raw-cased company|title from the pre-lowercasing build) to the canonical one
     * (mirrors the Python migrate_decision_keys.py). Keeps the later-ts record on a
     * collision. Returns true if anything moved. Runs at load; the sync merge below
     * canonicalizes incoming remote keys too, so raw keys can't creep back in.
     */
    private fun migrateDecisionKeys(obj: JSONObject): Boolean {
        var changed = false
        for (oldKey in obj.keys().asSequence().toList()) {
            val rec = obj.optJSONObject(oldKey) ?: continue
            val newKey = canonicalKey(oldKey, rec)
            if (newKey == oldKey) continue
            val existing = obj.optJSONObject(newKey)
            if (existing == null || rec.optString("ts") >= existing.optString("ts")) {
                obj.put(newKey, rec)
            }
            obj.remove(oldKey)
            changed = true
        }
        return changed
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

    /**
     * Append new jobs, deduplicating against existing rows by the job's
     * cross-device identity (decisionKey: company|title, URL fallback) — the same
     * key the sync layer and the swipe decisions use, so the same role from two
     * sources is one job on every device. Mirrors save_jobs in agent.py.
     */
    fun saveJobs(jobs: List<Job>): List<Job> {
        val existing = loadJobs().toMutableList()
        val known = existing.map { it.decisionKey }.filter { it.isNotEmpty() }.toMutableSet()
        val added = mutableListOf<Job>()
        for (job in jobs) {
            val k = job.decisionKey
            if (k.isNotEmpty() && k in known) continue
            if (k.isNotEmpty()) known.add(k)
            existing.add(job)
            added.add(job)
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

    /**
     * Jobs marked applied — the set an inbox reply can be about. `applied` is
     * written by the desktop shell's /applied and arrives here through sync.
     */
    fun appliedJobs(): List<AppliedJob> = decisions.keys().asSequence().mapNotNull { key ->
        val rec = decisions.optJSONObject(key) ?: return@mapNotNull null
        if (rec.optString("decision") != "applied") return@mapNotNull null
        AppliedJob(
            key = key,
            company = rec.optString("company"),
            title = rec.optString("title"),
        )
    }.filter { it.company.isNotBlank() }.toList()

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
            Sync.pushReplies(replies.asJson())
            return "sync: seeded remote"
        }
        var mergedDecisions = 0
        for ((remoteKey, record) in remoteDecisions) {
            // Canonicalize the incoming key so a raw/legacy remote key never
            // re-enters local state (and thus never gets pushed back up).
            val key = canonicalKey(remoteKey, record)
            val mine = decisions.optJSONObject(key)
            if (mine == null || record.optString("ts") > mine.optString("ts")) {
                decisions.put(key, record)
                mergedDecisions++
            }
        }
        if (mergedDecisions > 0) persistDecisions()
        val newJobs = saveJobs(remoteJobs)
        // Replies: this device is the only writer, but another install may have
        // scanned — merge remote in first, then push the union back.
        replies.mergeRemote(Sync.pullReplies())
        // Push back what only this device knows.
        Sync.pushDecisions(decisions)
        val remoteKeys = remoteJobs.map { Sync.jobKey(it) }.toSet()
        Sync.pushJobs(loadJobs().filter { Sync.jobKey(it) !in remoteKeys })
        Sync.pushReplies(replies.asJson())
        return "sync: +${newJobs.size} jobs, +$mergedDecisions decisions"
    }
}
