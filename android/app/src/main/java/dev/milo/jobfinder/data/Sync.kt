package dev.milo.jobfinder.data

import com.google.android.gms.tasks.Tasks
import com.google.firebase.auth.FirebaseAuth
import com.google.firebase.auth.FirebaseAuthException
import com.google.firebase.firestore.FirebaseFirestore
import org.json.JSONObject
import java.security.MessageDigest

/**
 * Cross-device sync via Cloud Firestore (Firebase SDK) — the Kotlin twin of
 * the repo's sync.py. Same collections, same document ids:
 *
 *   jobs/<key>          job records            (content-addressed, merged)
 *   decisions/<key>     swipe decisions        (last ts wins)
 *   descriptions/<key>  fetched posting text   (pushed on fetch, pulled on miss)
 *   replies/<sha1 msgid> classified inbox replies (this app writes, Python reads)
 *
 * <key> = sha1(decisionKey) = sha1("company|title", URL fallback); records carry the original
 * key. Auth is the baked-in email/password user (Keys.kt); the project config
 * comes from google-services.json. Disabled (all no-ops) when the email is
 * blank. All methods are blocking — call from Dispatchers.IO.
 */
object Sync {

    val enabled: Boolean
        get() = Keys.FIREBASE_EMAIL.isNotBlank() && Keys.FIREBASE_PASSWORD.isNotBlank()

    private var authFailed = false
    private const val BATCH = 400 // Firestore batch limit is 500 writes

    fun sha1(s: String): String =
        MessageDigest.getInstance("SHA-1").digest(s.toByteArray())
            .joinToString("") { "%02x".format(it) }

    fun jobKey(job: Job): String = sha1(job.decisionKey)

    /** Signed-in Firestore handle, or null when sync is off/unreachable. */
    @Synchronized
    private fun db(): FirebaseFirestore? {
        if (!enabled || authFailed) return null
        val auth = FirebaseAuth.getInstance()
        if (auth.currentUser == null) {
            try {
                Tasks.await(
                    auth.signInWithEmailAndPassword(Keys.FIREBASE_EMAIL, Keys.FIREBASE_PASSWORD)
                )
            } catch (e: Exception) {
                if (e.cause is FirebaseAuthException) {
                    authFailed = true // bad credentials won't fix themselves
                }
                return null
            }
        }
        return FirebaseFirestore.getInstance()
    }

    // --- records --------------------------------------------------------------

    private fun jobRecord(job: Job): Map<String, Any> = buildMap {
        put("key", job.decisionKey)
        put("title", job.title)
        put("company", job.company)
        if (job.url.isNotBlank()) put("url", job.url)
        if (job.location.isNotBlank()) put("location", job.location)
        if (job.salary.isNotBlank() && job.salary != "Not listed") put("salary", job.salary)
        if (job.postedDate.isNotBlank()) put("posted_date", job.postedDate)
        if (job.wfhPolicy.isNotBlank() && job.wfhPolicy != "Unknown") put("wfh_policy", job.wfhPolicy)
        if (job.source.isNotBlank()) put("source", job.source)
        if (job.fitScore >= 0) put("fit_score", job.fitScore)
        if (job.startupScore >= 0) put("startup_score", job.startupScore)
        if (job.climateScore >= 0) put("climate_score", job.climateScore)
        if (job.reason.isNotBlank()) put("reason", job.reason)
    }

    private fun mapToJson(data: Map<String, Any?>): JSONObject {
        val o = JSONObject()
        for ((k, v) in data) if (v != null) o.put(k, v)
        return o
    }

    // --- domain operations -----------------------------------------------------

    /** Upsert jobs (and any in-memory descriptions) in write batches. */
    fun pushJobs(jobs: List<Job>) {
        if (jobs.isEmpty()) return
        val db = db() ?: return
        jobs.chunked(BATCH / 2).forEach { chunk ->
            val batch = db.batch()
            for (job in chunk) {
                batch.set(db.collection("jobs").document(jobKey(job)), jobRecord(job))
                val desc = job.desc
                // Descriptions are keyed by URL (page content is per-URL), not by
                // the job identity — matches getDescription / pushDescription and
                // the Python side (sync.py norm_key(url)).
                if (!desc.isNullOrBlank() && job.url.isNotBlank()) {
                    batch.set(
                        db.collection("descriptions").document(sha1(Job.normalizeUrl(job.url))),
                        mapOf("url" to job.url, "description" to desc),
                    )
                }
            }
            Tasks.await(batch.commit())
        }
    }

    fun pullJobs(): List<Job> {
        val db = db() ?: return emptyList()
        return try {
            Tasks.await(db.collection("jobs").get()).documents
                .mapNotNull { it.data?.let { d -> Job.fromJson(mapToJson(d)) } }
                .filter { it.title.isNotBlank() }
        } catch (e: Exception) {
            emptyList()
        }
    }

    /** Write (or, on undo, delete) one swipe decision. */
    fun pushDecision(localKey: String, record: JSONObject?) {
        val db = db() ?: return
        val doc = db.collection("decisions").document(sha1(localKey))
        try {
            if (record == null) {
                Tasks.await(doc.delete())
            } else {
                val data = mutableMapOf<String, Any>("key" to localKey)
                for (k in record.keys()) data[k] = record.get(k)
                Tasks.await(doc.set(data))
            }
        } catch (e: Exception) { /* best-effort */ }
    }

    fun pushDecisions(decisions: JSONObject) {
        if (decisions.length() == 0) return
        val db = db() ?: return
        decisions.keys().asSequence().toList().chunked(BATCH).forEach { chunk ->
            val batch = db.batch()
            for (localKey in chunk) {
                val record = decisions.optJSONObject(localKey) ?: continue
                val data = mutableMapOf<String, Any>("key" to localKey)
                for (k in record.keys()) data[k] = record.get(k)
                batch.set(db.collection("decisions").document(sha1(localKey)), data)
            }
            try {
                Tasks.await(batch.commit())
            } catch (e: Exception) { /* best-effort */ }
        }
    }

    /** Remote decisions as localKey → record. */
    fun pullDecisions(): Map<String, JSONObject> {
        val db = db() ?: return emptyMap()
        return try {
            val out = mutableMapOf<String, JSONObject>()
            for (doc in Tasks.await(db.collection("decisions").get()).documents) {
                val data = doc.data ?: continue
                val localKey = data["key"] as? String ?: continue
                val record = mapToJson(data)
                record.remove("key")
                out[localKey] = record
            }
            out
        } catch (e: Exception) {
            emptyMap()
        }
    }

    /**
     * Upsert classified inbox replies. Document id is sha1(rfc822 message id), so
     * re-classifying a message (once API credits are back, say) overwrites its
     * record instead of adding a second one. This app is the only writer; the
     * desktop tool reads the collection to show its interview banner.
     */
    fun pushReplies(replies: JSONObject) {
        if (replies.length() == 0) return
        val db = db() ?: return
        replies.keys().asSequence().toList().chunked(BATCH).forEach { chunk ->
            val batch = db.batch()
            for (id in chunk) {
                val record = replies.optJSONObject(id) ?: continue
                val data = mutableMapOf<String, Any>("id" to id)
                for (k in record.keys()) data[k] = record.get(k)
                batch.set(db.collection("replies").document(id), data)
            }
            try {
                Tasks.await(batch.commit())
            } catch (e: Exception) { /* best-effort */ }
        }
    }

    /** Remote replies as id → record. */
    fun pullReplies(): Map<String, JSONObject> {
        val db = db() ?: return emptyMap()
        return try {
            val out = mutableMapOf<String, JSONObject>()
            for (doc in Tasks.await(db.collection("replies").get()).documents) {
                val data = doc.data ?: continue
                val record = mapToJson(data)
                record.remove("id")
                out[doc.id] = record
            }
            out
        } catch (e: Exception) {
            emptyMap()
        }
    }

    fun getDescription(url: String): String? {
        val db = db() ?: return null
        return try {
            Tasks.await(db.collection("descriptions").document(sha1(Job.normalizeUrl(url))).get())
                .getString("description")?.ifBlank { null }
        } catch (e: Exception) {
            null
        }
    }

    fun pushDescription(url: String, description: String) {
        if (url.isBlank() || description.isBlank()) return
        val db = db() ?: return
        try {
            Tasks.await(
                db.collection("descriptions").document(sha1(Job.normalizeUrl(url)))
                    .set(mapOf("url" to url, "description" to description))
            )
        } catch (e: Exception) { /* best-effort */ }
    }
}
