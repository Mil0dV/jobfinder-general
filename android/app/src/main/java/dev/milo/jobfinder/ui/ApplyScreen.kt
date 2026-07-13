package dev.milo.jobfinder.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.milo.jobfinder.AppViewModel
import dev.milo.jobfinder.GenKind
import kotlinx.coroutines.delay

private const val SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

@Composable
fun ApplyScreen(
    vm: AppViewModel,
    onOpenUrl: (String) -> Unit,
    onCopy: (String) -> Unit,
    onShare: (String) -> Unit,
) {
    var question by remember { mutableStateOf("") }
    var extraContext by remember { mutableStateOf("") }
    var showQuestionField by remember { mutableStateOf(false) }
    var showContextField by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .padding(16.dp),
    ) {
        TermText(vm.applyHeader.ifBlank { "Apply" }, bold = true, fontSize = 15.sp)
        if (vm.applyJob?.isPearl == true) {
            TermText(
                "🦪 pearl supermatch — cover letter comes as a drunk draft",
                color = Term.magenta, fontSize = 11.sp,
            )
        }
        val desc = vm.applyDescription
        when {
            desc == null -> TermText("fetching job description…", color = Term.dim, fontSize = 11.sp)
            desc.isBlank() -> TermText(
                "couldn't extract the posting text — generating from the URL/context only",
                color = Term.yellow, fontSize = 11.sp,
            )
            else -> TermText("description loaded (${desc.length} chars)", color = Term.dim, fontSize = 11.sp)
        }
        HorizontalDivider(
            color = Term.dim.copy(alpha = 0.4f), thickness = 1.dp,
            modifier = Modifier.padding(vertical = 8.dp),
        )

        // Action words.
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
            WordButton("cover letter", color = Term.green) {
                showQuestionField = false
                showContextField = true
            }
            WordButton("answer", color = Term.cyan) {
                showContextField = false
                showQuestionField = true
            }
            WordButton("salary", color = Term.yellow) {
                showQuestionField = false
                showContextField = false
                vm.generateSalary()
            }
            WordButton("open", color = Term.dim, enabled = vm.applyUrl.isNotBlank()) {
                onOpenUrl(vm.applyUrl)
            }
        }

        if (showContextField) {
            TermInput(
                value = extraContext,
                onChange = { extraContext = it },
                label = "extra context (optional) — e.g. mention terraform",
            )
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                WordButton("write it", color = Term.green) {
                    showContextField = false
                    vm.generateCoverLetter(extraContext)
                }
            }
        }
        if (showQuestionField) {
            TermInput(
                value = question,
                onChange = { question = it },
                label = "application question",
            )
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                WordButton("answer it", color = Term.cyan, enabled = question.isNotBlank()) {
                    showQuestionField = false
                    vm.generateAnswer(question)
                }
            }
        }

        // Output area.
        var frame by remember { mutableStateOf(0) }
        LaunchedEffect(vm.genRunning) {
            while (vm.genRunning) {
                delay(90)
                frame++
            }
        }
        val scroll = rememberScrollState()
        LaunchedEffect(vm.genOutput) { scroll.scrollTo(scroll.maxValue) }

        Column(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(scroll),
        ) {
            if (vm.genKind != GenKind.NONE) {
                val title = when (vm.genKind) {
                    GenKind.COVER_LETTER -> "Cover letter"
                    GenKind.ANSWER -> "Answer"
                    GenKind.SALARY -> "Salary estimate"
                    GenKind.NONE -> ""
                }
                val spin = if (vm.genRunning) " ${SPINNER[frame % SPINNER.length]}" else ""
                TermText("── $title$spin ──", color = Term.dim, fontSize = 11.sp)
                Spacer(Modifier.height(6.dp))
            }
            vm.genError?.let { TermText("Error: $it", color = Term.red, fontSize = 12.sp) }
            if (vm.genOutput.isNotBlank()) {
                TermText(vm.genOutput, fontSize = 13.sp)
                if (!vm.genRunning) {
                    Spacer(Modifier.height(6.dp))
                    TermText(
                        "(${vm.genOutput.trim().split(Regex("\\s+")).size} words)",
                        color = Term.dim, fontSize = 11.sp,
                    )
                }
            } else if (vm.genKind == GenKind.NONE && vm.genError == null) {
                Spacer(Modifier.height(12.dp))
                TermText(
                    "cover letter — write a letter for this job\n" +
                        "answer — answer an application question\n" +
                        "salary — estimate the pay range",
                    color = Term.dim, fontSize = 11.sp,
                )
            }
        }

        // Footer.
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
            if (vm.genRunning) {
                WordButton("stop", color = Term.red) { vm.stopGeneration() }
            }
            if (vm.genOutput.isNotBlank() && !vm.genRunning) {
                WordButton("copy", color = Term.green) { onCopy(vm.genOutput) }
                WordButton("share", color = Term.cyan) { onShare(vm.genOutput) }
                WordButton("retry", color = Term.dim) {
                    when (vm.genKind) {
                        GenKind.COVER_LETTER -> vm.generateCoverLetter(extraContext)
                        GenKind.ANSWER -> vm.generateAnswer(question)
                        GenKind.SALARY -> vm.generateSalary()
                        GenKind.NONE -> {}
                    }
                }
            }
        }
    }
}

@Composable
private fun TermInput(value: String, onChange: (String) -> Unit, label: String) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        textStyle = termStyle.copy(fontSize = 13.sp),
        label = { TermText(label, color = Term.dim, fontSize = 11.sp) },
        colors = OutlinedTextFieldDefaults.colors(
            focusedTextColor = Term.fg,
            unfocusedTextColor = Term.fg,
            focusedBorderColor = Term.green,
            unfocusedBorderColor = Term.dim,
            cursorColor = Term.cyan,
        ),
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
    )
}
