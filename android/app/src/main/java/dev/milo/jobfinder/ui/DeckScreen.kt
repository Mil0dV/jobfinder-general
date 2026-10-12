package dev.milo.jobfinder.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.milo.jobfinder.AppViewModel
import dev.milo.jobfinder.data.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
fun DeckScreen(
    vm: AppViewModel,
    onOpenUrl: (String) -> Unit,
    onApply: (Job) -> Unit,
    onFinish: () -> Unit,
) {
    vm.deckRefresh // subscribe: async desc/company loads bump this
    val job = vm.deck.getOrNull(vm.deckIndex)

    // Tick every 200ms while an undo window is open, to update the countdown.
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(vm.undoJob) {
        while (vm.undoJob != null) {
            now = System.currentTimeMillis()
            delay(200)
        }
    }
    val undoLeft = vm.undoJob?.let { ((vm.undoDeadline - now) / 1000f).coerceAtLeast(0f) }
        ?.takeIf { it > 0f }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .padding(horizontal = 14.dp, vertical = 10.dp),
    ) {
        if (job == null) {
            Column(
                modifier = Modifier.weight(1f).fillMaxWidth(),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                TermText("Nothing new to review — every job", color = Term.dim)
                TermText("here is already decided. 🎉", color = Term.dim)
                Spacer(Modifier.height(20.dp))
                WordButton("back", color = Term.dim) { onFinish() }
            }
            return
        }

        // Header: job 3/17   ♥ 2 liked · ✗ 4 passed
        Row {
            TermText("job ${vm.deckIndex + 1}/${vm.deck.size}   ", color = Term.dim, fontSize = 12.sp)
            TermText("♥ ${vm.liked.size}", color = Term.green, fontSize = 12.sp)
            TermText(" liked · ", color = Term.dim, fontSize = 12.sp)
            TermText("✗ ${vm.passedCount}", color = Term.red, fontSize = 12.sp)
            TermText(" passed", color = Term.dim, fontSize = 12.sp)
        }
        Spacer(Modifier.height(8.dp))

        SwipeableCard(
            job = job,
            vm = vm,
            modifier = Modifier.weight(1f),
            onOpenUrl = onOpenUrl,
            onApply = onApply,
        )

        // Bottom controls: underlined words, TUI style.
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.Center,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            WordButton("pass", color = Term.red) { vm.decide(false) }
            WordButton("desc", color = Term.dim) {
                vm.expanded = if (vm.expanded == "desc") null else "desc"
            }
            WordButton("company", color = Term.dim) {
                vm.expanded = if (vm.expanded == "company") null else "company"
                if (vm.expanded == "company") vm.loadCompanyInfo(job)
            }
            WordButton("open", color = Term.dim, enabled = job.url.isNotBlank()) {
                onOpenUrl(job.url)
            }
            WordButton("apply", color = Term.cyan) { onApply(job) }
            WordButton("like", color = Term.green) { vm.decide(true) }
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.Center,
        ) {
            if (undoLeft != null) {
                WordButton("↶ undo (${undoLeft.toInt() + 1}s)", color = Term.magenta) { vm.undo() }
            } else {
                WordButton("finish", color = Term.dim) { onFinish() }
            }
        }
    }
}

@Composable
private fun SwipeableCard(
    job: Job,
    vm: AppViewModel,
    modifier: Modifier = Modifier,
    onOpenUrl: (String) -> Unit,
    onApply: (Job) -> Unit,
) {
    val scope = rememberCoroutineScope()
    val offsetX = remember(job) { Animatable(0f) }
    val screenWidthPx = LocalConfiguration.current.screenWidthDp * 3f // generous px estimate
    val threshold = screenWidthPx / 9f

    Box(
        modifier = modifier
            .fillMaxWidth()
            .graphicsLayer {
                translationX = offsetX.value
                rotationZ = offsetX.value / 60f
            }
            .pointerInput(job) {
                detectDragGestures(
                    onDragEnd = {
                        scope.launch {
                            when {
                                offsetX.value > threshold -> {
                                    offsetX.animateTo(screenWidthPx, tween(180))
                                    vm.decide(true)
                                }
                                offsetX.value < -threshold -> {
                                    offsetX.animateTo(-screenWidthPx, tween(180))
                                    vm.decide(false)
                                }
                                else -> offsetX.animateTo(0f, tween(220))
                            }
                        }
                    },
                    onDrag = { change, dragAmount ->
                        change.consume()
                        scope.launch { offsetX.snapTo(offsetX.value + dragAmount.x) }
                    },
                )
            }
            .border(1.dp, Term.dim.copy(alpha = 0.5f))
            .padding(12.dp),
    ) {
        CardContent(job, vm)

        // LIKE / PASS stamps while dragging.
        val drag = offsetX.value
        if (drag > 30f) {
            TermText(
                "♥ LIKE", color = Term.green, bold = true, fontSize = 22.sp,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .padding(6.dp)
                    .alpha((drag / 300f).coerceIn(0f, 1f)),
            )
        } else if (drag < -30f) {
            TermText(
                "✗ PASS", color = Term.red, bold = true, fontSize = 22.sp,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(6.dp)
                    .alpha((-drag / 300f).coerceIn(0f, 1f)),
            )
        }
    }
}

@Composable
private fun CardContent(job: Job, vm: AppViewModel) {
    Column(modifier = Modifier.fillMaxSize()) {
        if (job.isPearl) {
            TermText("🦪 PEARL — supermatch", color = Term.magenta, bold = true, fontSize = 13.sp)
        }
        // From the best-general-roles deck (or a saved one): flagged everywhere.
        if (job.climateScore in 0..1) {
            TermText("💼 general role — no climate angle", color = Term.yellow, bold = true, fontSize = 13.sp)
        }
        TermText(job.title, bold = true, fontSize = 16.sp)
        TermText(job.company, color = Term.cyan, fontSize = 14.sp)
        CardRule()
        TermText("📍 ${job.location.ifBlank { job.wfhPolicy.ifBlank { "?" } }}", fontSize = 12.sp)
        if (job.salary.isNotBlank() && job.salary != "Not listed") {
            TermText("💰 ${job.salary}", fontSize = 12.sp)
        }
        val bits = buildList {
            if (job.postedDate.isNotBlank()) add("posted ${job.postedDate}")
            if (job.wfhPolicy.isNotBlank() && job.wfhPolicy != "Unknown") add(job.wfhPolicy)
            if (job.source.isNotBlank()) add("via ${job.source}")
        }
        if (bits.isNotEmpty()) TermText(bits.joinToString(" · "), color = Term.dim, fontSize = 11.sp)
        Row(verticalAlignment = Alignment.CenterVertically) {
            TermText("fit ", fontSize = 11.sp)
            TermText(stars(job.fitScore), fontSize = 11.sp)
            TermText("  startup ", fontSize = 11.sp)
            TermText(stars(job.startupScore), fontSize = 11.sp)
            TermText("  climate ", fontSize = 11.sp)
            TermText(stars(job.climateScore), fontSize = 11.sp)
        }
        if (job.reason.isNotBlank()) TermText(job.reason, color = Term.dim, fontSize = 11.sp)
        CardRule()

        when (vm.expanded) {
            "desc" -> ExpandedBody("Role description", job.desc ?: "loading…", Modifier.weight(1f))
            "company" -> ExpandedBody("Company", job.companyInfo ?: "loading…", Modifier.weight(1f))
            else -> {
                // Preview of the role description — whatever fits, scrollable.
                val desc = job.desc ?: job.snippet
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .verticalScroll(rememberScrollState()),
                ) {
                    if (desc.isNotBlank()) {
                        TermText(desc, color = Term.fg.copy(alpha = 0.85f), fontSize = 11.sp)
                    } else {
                        TermText("loading description…", color = Term.dim, fontSize = 11.sp)
                    }
                }
                CardRule()
                TermText(
                    "← swipe to pass · swipe to like →",
                    color = Term.dim, fontSize = 10.sp,
                    modifier = Modifier.align(Alignment.CenterHorizontally),
                )
            }
        }
    }
}

@Composable
private fun ExpandedBody(title: String, body: String, modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxWidth()) {
        TermText(title, bold = true, fontSize = 12.sp)
        Spacer(Modifier.height(4.dp))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = false)
                .verticalScroll(rememberScrollState()),
        ) {
            TermText(body, fontSize = 11.sp)
        }
    }
}

@Composable
private fun CardRule() {
    HorizontalDivider(
        color = Term.dim.copy(alpha = 0.4f),
        thickness = 1.dp,
        modifier = Modifier.padding(vertical = 6.dp),
    )
}
