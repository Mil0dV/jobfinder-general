package dev.milo.jobfinder

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import dev.milo.jobfinder.data.DescFetcher
import dev.milo.jobfinder.data.Generator
import dev.milo.jobfinder.data.Job
import dev.milo.jobfinder.data.Profile
import dev.milo.jobfinder.data.SearchEngine
import dev.milo.jobfinder.data.Store
import dev.milo.jobfinder.data.Sync
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job as CoroutineJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

sealed class Screen {
    data object Splash : Screen()
    data object Menu : Screen()
    data object RoleInput : Screen()
    data object Searching : Screen()
    data object Deck : Screen()
    data object Summary : Screen()
    data object Apply : Screen()
}

/** What the apply screen is generating right now. */
enum class GenKind { NONE, COVER_LETTER, ANSWER, SALARY }

class AppViewModel(app: Application) : AndroidViewModel(app) {

    private val filesDir: File = app.filesDir
    val store = Store(filesDir)
    private val descFetcher = DescFetcher(File(filesDir, "job_descriptions.json"))
    private val engine = SearchEngine(descFetcher)
    private val generator = Generator(app, descFetcher)

    // One-line sync summary shown on the menu (null = sync off or still running).
    var syncStatus by mutableStateOf<String?>(null)
        private set

    init {
        viewModelScope.launch(Dispatchers.IO) {
            val summary = try {
                store.syncWithRemote()
            } catch (e: Exception) {
                "sync failed: ${e.message}"
            }
            withContext(Dispatchers.Main) { syncStatus = summary }
        }
    }

    // --- navigation ------------------------------------------------------------

    private val backStack = mutableStateListOf<Screen>(Screen.Splash)
    val screen: Screen get() = backStack.last()

    fun navigate(s: Screen) {
        backStack.add(s)
    }

    fun replace(s: Screen) {
        backStack.removeAt(backStack.lastIndex)
        backStack.add(s)
    }

    /** Returns false when there is nowhere to go back to (let the system handle it). */
    fun back(): Boolean {
        if (backStack.size <= 1) return false
        if (screen == Screen.Apply) stopGeneration()
        if (screen == Screen.Searching) cancelSearch()
        backStack.removeAt(backStack.lastIndex)
        if (screen == Screen.Splash) replace(Screen.Menu) // never land back on the splash
        return true
    }

    fun toMenu() {
        backStack.clear()
        backStack.add(Screen.Menu)
    }

    // --- search ----------------------------------------------------------------

    var searchQuery by mutableStateOf(Profile.PROFILE_QUERY)
        private set
    var searchPhase by mutableStateOf("starting")
    var searchDone by mutableStateOf(0)
    var searchTotal by mutableStateOf(1)
    var searchDetail by mutableStateOf("")
    val sourceCounts = mutableStateMapOf<String, String>()
    var searchError by mutableStateOf<String?>(null)
    private var searchJob: CoroutineJob? = null

    fun startSearch(query: String) {
        searchQuery = query
        searchPhase = "starting"
        searchDone = 0
        searchTotal = 1
        searchDetail = ""
        searchError = null
        sourceCounts.clear()
        navigate(Screen.Searching)
        searchJob = viewModelScope.launch {
            try {
                val jobs = engine.search(query) { p ->
                    viewModelScope.launch(Dispatchers.Main) {
                        searchPhase = p.phase
                        searchDone = p.done
                        searchTotal = p.total
                        searchDetail = p.detail
                        if (p.phase == "sources" && ":" in p.detail) {
                            val name = p.detail.substringBeforeLast(":")
                            sourceCounts[name] = p.detail.substringAfterLast(":")
                        }
                    }
                }
                withContext(Dispatchers.IO) {
                    val added = store.saveJobs(jobs)
                    try {
                        Sync.pushJobs(added)
                    } catch (e: Exception) { /* sync is best-effort */ }
                }
                openDeck(jobs.filter { !store.isDecided(it) })
                replace(Screen.Deck)
            } catch (e: Exception) {
                searchError = e.message ?: "search failed"
            }
        }
    }

    fun cancelSearch() {
        searchJob?.cancel()
        searchJob = null
    }

    // --- deck ---------------------------------------------------------------------

    val deck = mutableStateListOf<Job>()
    var deckIndex by mutableStateOf(0)
    val liked = mutableStateListOf<Job>()
    var passedCount by mutableStateOf(0)
        private set
    var undoJob by mutableStateOf<Job?>(null)
        private set
    var undoDeadline by mutableStateOf(0L)
        private set
    private var undoWasLiked = false
    // Card expansion: null | "desc" | "company"
    var expanded by mutableStateOf<String?>(null)

    fun openDeck(jobs: List<Job>) {
        deck.clear()
        // Pearls (supermatches) first, then most recent; undated jobs at the end.
        deck.addAll(jobs.sortedWith(compareByDescending<Job> { it.isPearl }
            .thenByDescending { it.postedDate }
            .thenByDescending { it.fitScore }))
        deckIndex = 0
        liked.clear()
        passedCount = 0
        undoJob = null
        expanded = null
        prefetchDescriptions()
    }

    fun reviewSavedJobs() {
        openDeck(store.undecidedSavedJobs())
        navigate(Screen.Deck)
    }

    fun decide(likedIt: Boolean) {
        val job = deck.getOrNull(deckIndex) ?: return
        val record = store.decide(job, likedIt)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                Sync.pushDecision(job.decisionKey, record)
            } catch (e: Exception) { /* sync is best-effort */ }
        }
        if (likedIt) liked.add(job) else passedCount++
        undoJob = job
        undoWasLiked = likedIt
        undoDeadline = System.currentTimeMillis() + 5000
        deckIndex++
        expanded = null
        prefetchDescriptions()
        if (deckIndex >= deck.size) replace(Screen.Summary)
    }

    fun undo() {
        val job = undoJob ?: return
        if (System.currentTimeMillis() > undoDeadline) {
            undoJob = null
            return
        }
        store.undecide(job)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                Sync.pushDecision(job.decisionKey, null)
            } catch (e: Exception) { /* sync is best-effort */ }
        }
        if (undoWasLiked) liked.remove(job) else passedCount--
        if (screen == Screen.Summary) replace(Screen.Deck)
        deckIndex = deck.indexOf(job).coerceAtLeast(0)
        undoJob = null
        expanded = null
    }

    private fun prefetchDescriptions() {
        for (i in deckIndex until minOf(deckIndex + 3, deck.size)) {
            val job = deck[i]
            if (job.desc == null && job.url.startsWith("http")) {
                viewModelScope.launch(Dispatchers.IO) {
                    val text = try {
                        descFetcher.fetch(job.url)
                    } catch (e: Exception) {
                        ""
                    }
                    withContext(Dispatchers.Main) {
                        job.desc = text.ifBlank { job.snippet.ifBlank { "(no description available)" } }
                        if (deck.getOrNull(deckIndex) == job) deckRefresh++
                    }
                }
            }
        }
    }

    // Bumped when async data lands on the visible card, to trigger recomposition
    // (Job fields are plain vars, not Compose state).
    var deckRefresh by mutableStateOf(0)

    fun loadCompanyInfo(job: Job) {
        if (job.companyInfo != null) return
        job.companyInfo = "loading…"
        viewModelScope.launch(Dispatchers.IO) {
            val info = generator.companyInfo(job.company)
            withContext(Dispatchers.Main) {
                job.companyInfo = info
                deckRefresh++
            }
        }
    }

    // --- apply screen ------------------------------------------------------------------

    var applyJob by mutableStateOf<Job?>(null)
        private set
    var applyUrl by mutableStateOf("")
        private set
    var applyHeader by mutableStateOf("")
    var applyDescription by mutableStateOf<String?>(null) // null = still fetching
    var genKind by mutableStateOf(GenKind.NONE)
    var genOutput by mutableStateOf("")
    var genRunning by mutableStateOf(false)
    var genError by mutableStateOf<String?>(null)
    private var genStopped = false

    /** Open the apply screen for a deck/saved job. */
    fun openApply(job: Job) {
        applyJob = job
        applyUrl = job.url
        applyHeader = "${job.title} @ ${job.company}"
        applyDescription = job.desc
        resetGeneration()
        navigate(Screen.Apply)
        if (applyDescription == null && job.url.startsWith("http")) fetchApplyDescription(job.url)
    }

    /** Open the apply screen from a shared URL (share target entry point). */
    fun openSharedUrl(url: String) {
        applyJob = null
        applyUrl = url
        applyHeader = url.substringAfter("://").take(60)
        applyDescription = null
        resetGeneration()
        // Whatever the app was doing, a share lands on the apply screen over the menu.
        backStack.clear()
        backStack.add(Screen.Menu)
        backStack.add(Screen.Apply)
        fetchApplyDescription(url, extractMeta = true)
    }

    private fun fetchApplyDescription(url: String, extractMeta: Boolean = false) {
        viewModelScope.launch(Dispatchers.IO) {
            val text = try {
                generator.fetchDescription(url)
            } catch (e: Exception) {
                ""
            }
            var header: String? = null
            if (extractMeta) {
                // A shared URL becomes a saved job (and syncs to the other devices).
                val (company, role) = if (text.isNotBlank()) generator.extractJobMeta(text)
                else "Unknown" to "Role"
                if (company != "Unknown") header = "$role @ $company"
                val job = Job.make(
                    title = if (role != "Role") role else url.substringAfter("://").take(60),
                    company = company,
                    url = url,
                    postedDate = java.time.LocalDate.now().toString(),
                    source = "shared",
                ).also { it.desc = text.ifBlank { null } }
                try {
                    Sync.pushJobs(store.saveJobs(listOf(job)))
                } catch (e: Exception) { /* sync is best-effort */ }
            }
            withContext(Dispatchers.Main) {
                applyDescription = text
                applyJob?.let { if (it.desc == null && text.isNotBlank()) it.desc = text }
                header?.let { applyHeader = it }
            }
        }
    }

    private fun resetGeneration() {
        stopGeneration()
        genKind = GenKind.NONE
        genOutput = ""
        genError = null
    }

    fun stopGeneration() {
        genStopped = true
        genRunning = false
    }

    private fun runGeneration(kind: GenKind, block: (shouldStop: () -> Boolean, onDelta: (String) -> Unit) -> String) {
        stopGeneration()
        genStopped = false
        genKind = kind
        genOutput = ""
        genError = null
        genRunning = true
        viewModelScope.launch(Dispatchers.IO) {
            try {
                block({ genStopped }) { delta ->
                    viewModelScope.launch(Dispatchers.Main) { genOutput += delta }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) { genError = e.message ?: "generation failed" }
            } finally {
                withContext(Dispatchers.Main) { genRunning = false }
            }
        }
    }

    private fun applyJobContext(): String =
        generator.buildJobContext(applyJob, applyDescription ?: "")

    fun generateCoverLetter(extraContext: String) = runGeneration(GenKind.COVER_LETTER) { stop, delta ->
        // Pearl supermatches get the rougher, riskier "drunk draft" — Milo
        // rewrites those by hand anyway.
        generator.streamCoverLetter(
            applyJobContext(), extraContext,
            drunk = applyJob?.isPearl == true,
            shouldStop = stop, onDelta = delta,
        )
    }

    fun generateAnswer(question: String) = runGeneration(GenKind.ANSWER) { stop, delta ->
        generator.streamAnswer(question, applyJobContext(), stop, delta)
    }

    fun generateSalary() = runGeneration(GenKind.SALARY) { stop, delta ->
        generator.streamSalary(applyJobContext(), stop, delta)
    }
}
