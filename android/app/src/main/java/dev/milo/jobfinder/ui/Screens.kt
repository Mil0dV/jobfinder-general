package dev.milo.jobfinder.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.milo.jobfinder.AppViewModel
import dev.milo.jobfinder.Screen
import dev.milo.jobfinder.data.Job
import dev.milo.jobfinder.data.Profile
import kotlinx.coroutines.delay

private const val SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
private val GLOBES = listOf("🌍", "🌎", "🌏")
private val PLANTS = listOf("🌱", "🌿", "🪴", "🌳")

// --- menu -----------------------------------------------------------------------

@Composable
fun MenuScreen(vm: AppViewModel) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        TermText("🌱 Climate & Green Tech Job Finder", bold = true)
        Spacer(Modifier.height(8.dp))
        TermText(
            "germany remote · netherlands remote · switzerland hybrid/remote",
            color = Term.dim, fontSize = 11.sp,
        )
        Spacer(Modifier.height(32.dp))
        MenuItem("Search my target-role profile") { vm.startSearch(Profile.PROFILE_QUERY) }
        MenuItem("Search a specific role") { vm.navigate(Screen.RoleInput) }
        MenuItem("Review undecided saved jobs") { vm.reviewSavedJobs() }
        Spacer(Modifier.height(32.dp))
        TermText("or share a job URL to this app", color = Term.dim, fontSize = 11.sp)
        TermText("to write a cover letter / answers", color = Term.dim, fontSize = 11.sp)
        vm.syncStatus?.let {
            Spacer(Modifier.height(20.dp))
            TermText(it, color = Term.dim, fontSize = 10.sp)
        }
    }
}

@Composable
private fun MenuItem(label: String, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .clickable { onClick() }
            .padding(vertical = 10.dp),
    ) {
        TermText("▸ ", color = Term.green)
        TermText(label, bold = true)
    }
}

// --- role input --------------------------------------------------------------------

@Composable
fun RoleInputScreen(vm: AppViewModel) {
    var role by remember { mutableStateOf("") }
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        TermText("Role search", bold = true)
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = role,
            onValueChange = { role = it },
            textStyle = termStyle,
            label = { TermText("Job title", color = Term.dim, fontSize = 12.sp) },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(onSearch = {
                if (role.isNotBlank()) vm.startSearch(role.trim())
            }),
            colors = OutlinedTextFieldDefaults.colors(
                focusedTextColor = Term.fg,
                unfocusedTextColor = Term.fg,
                focusedBorderColor = Term.green,
                unfocusedBorderColor = Term.dim,
                cursorColor = Term.cyan,
            ),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(24.dp))
        Row {
            WordButton("search", color = Term.green, enabled = role.isNotBlank()) {
                vm.startSearch(role.trim())
            }
            WordButton("back", color = Term.dim) { vm.back() }
        }
    }
}

// --- search progress -----------------------------------------------------------------

private val PHASE_LABELS = mapOf(
    "sources" to "Querying job boards",
    "verify" to "Checking older postings are still online",
    "descriptions" to "Fetching job descriptions",
    "validate" to "Scoring with Claude",
    "done" to "Done",
)

private fun overallPercent(phase: String, done: Int, total: Int): Int {
    val t = maxOf(total, 1)
    return when (phase) {
        "sources" -> 2 + done * 38 / t
        "verify" -> 40 + done * 10 / t
        "descriptions" -> 50 + done * 25 / t
        "validate" -> 75 + done * 23 / t
        "done" -> 100
        else -> 1
    }
}

private val SOURCE_ORDER = listOf(
    "arbeitnow", "remoteok", "adzuna", "greenhouse", "lever",
    "climatetechlist", "climatebase", "jobsincarbon", "remoteimpact",
)

@Composable
fun SearchScreen(vm: AppViewModel) {
    var frame by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(90)
            frame++
        }
    }
    val pct = overallPercent(vm.searchPhase, vm.searchDone, vm.searchTotal)
    val spin = SPINNER[frame % SPINNER.length]
    val globe = GLOBES[(frame / 3) % GLOBES.size]
    val plant = PLANTS[minOf(PLANTS.size - 1, pct * PLANTS.size / 101)]
    val label = if (vm.searchQuery == Profile.PROFILE_QUERY) "your target-role profile"
    else "“${vm.searchQuery}”"

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .padding(20.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        TermText("$globe  Searching for $label", bold = true, fontSize = 13.sp)
        Spacer(Modifier.height(20.dp))
        val barW = 24
        val filled = barW * pct / 100
        Row(verticalAlignment = Alignment.CenterVertically) {
            TermText("$plant ", fontSize = 13.sp)
            TermText("█".repeat(filled), color = Term.green, fontSize = 13.sp)
            TermText("░".repeat(barW - filled), color = Term.dim, fontSize = 13.sp)
            TermText(" %3d%%".format(pct), fontSize = 13.sp)
        }
        Spacer(Modifier.height(20.dp))
        SOURCE_ORDER.forEach { name ->
            val count = vm.sourceCounts[name]
            Row {
                when {
                    count == null -> {
                        TermText("$spin ", color = Term.cyan, fontSize = 12.sp)
                        TermText(name, color = Term.dim, fontSize = 12.sp)
                    }
                    count == "error" -> {
                        TermText("✗ ", color = Term.red, fontSize = 12.sp)
                        TermText(name, fontSize = 12.sp)
                    }
                    else -> {
                        TermText("✓ ", color = Term.green, fontSize = 12.sp)
                        TermText(name, fontSize = 12.sp)
                        TermText("($count)", color = Term.dim, fontSize = 12.sp)
                    }
                }
            }
        }
        Spacer(Modifier.height(20.dp))
        val phaseLabel = PHASE_LABELS[vm.searchPhase] ?: "Warming up"
        val counter = if (vm.searchPhase in setOf("verify", "descriptions", "validate"))
            " (${vm.searchDone}/${vm.searchTotal})" else ""
        val detail = if (vm.searchPhase != "sources") " ${vm.searchDetail}" else ""
        TermText("$spin $phaseLabel$counter", color = Term.cyan, fontSize = 12.sp)
        if (detail.isNotBlank()) TermText(detail.trim(), color = Term.dim, fontSize = 11.sp)
        vm.searchError?.let {
            Spacer(Modifier.height(16.dp))
            TermText("Search failed: $it", color = Term.red, fontSize = 12.sp)
        }
        Spacer(Modifier.height(28.dp))
        WordButton(if (vm.searchError == null) "cancel" else "back", color = Term.dim) {
            vm.cancelSearch()
            vm.back()
        }
    }
}

// --- summary -----------------------------------------------------------------------

@Composable
fun SummaryScreen(vm: AppViewModel, onOpenJob: (Job) -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .padding(24.dp)
            .verticalScroll(rememberScrollState()),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        TermText("Review done", bold = true)
        Spacer(Modifier.height(16.dp))
        if (vm.liked.isEmpty()) {
            TermText("No jobs liked this round.", color = Term.dim)
        } else {
            TermText("♥ ${vm.liked.size} job(s) liked — tap one to apply:", color = Term.green)
            Spacer(Modifier.height(12.dp))
            vm.liked.forEach { job ->
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clickable { onOpenJob(job) }
                        .padding(vertical = 8.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    TermText(job.title, bold = true, fontSize = 13.sp)
                    TermText(job.company, color = Term.cyan, fontSize = 12.sp)
                }
            }
        }
        Spacer(Modifier.height(28.dp))
        WordButton("menu", color = Term.dim) { vm.toMenu() }
    }
}
