package dev.milo.jobfinder.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp

/** The startup card: the job witch holding up her verdict on a notepad (from tui.py). */
private val PAD = listOf(
    "      /\\ /\\ /\\ /\\ /\\ /\\  ",
    "     (  (  (  (  (  (  ) ",
    "    ┌────────────────────┐",
    "    │                    │",
    "    │   {B}You are a{R}        │",
    "    │                    │",
    "    │   {B}climate-{R}         │",
    "    │                    │",
    "    │   {B}job-{R}             │",
    "    │                    ├",
    "    │   {B}seeker{R}           │",
    "    │                    │",
    "    └────────────────────┘",
)

private val ART = listOf(
    " {C}▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄{R}",
    " {C}█ JOBFINDER ███████{R}                                  {D}~~~~~~~{R}",
    " {C}█ CLIMATE TUESDAY █{R}          {D}~~~~~~~~{R}                       {D}~~~~{R}",
    " {C}▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀{R}                        {D}▄▄▄▄▄▄▄▄▄{R}",
    "                                       {D}▄▄███████████████▄▄{R}",
    "                                    {D}▄███████████████████████▄{R}",
    "                                 {D}▄██████████████████████████████▄{R}",
    "                             {D}▄▄███████████████████████████████████▄▄{R}",
    "${PAD[0]} {D}▀▀▀▀▀▀▀▀▀▀▀███████████████▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀{R}",
    "${PAD[1]}             {D}███{G}█████████{D}███{R}",
    "${PAD[2]}          {D}██{G}█ ▄▄▄   ▄▄▄ █{D}██{R}",
    "${PAD[3]}          {D}██{G}█ ▀▄▀   ▀▄▀ █{D}██{R}",
    "${PAD[4]}          {D}██{G}█           █{D}██{R}",
    "${PAD[5]}           {D}█{G}██  ▄███▄  ██{D}█{R}",
    "${PAD[6]}            {G}▀██▄ ▀▀▀ ▄██▀{R}",
    "${PAD[7]}   {G}▄██▄      ▀███████▀{R}",
    "${PAD[8]}  {G}██▀▀██▄▄▄   {D}███████{R}",
    "${PAD[9]}{G}▄▄██     ▀▀█▄▄{D}██▀   ▀██{R}",
    "${PAD[10]} {G}▀▀{R}           {D}██     ██{R}",
    "${PAD[11]}              {D}██     ██{R}",
    "${PAD[12]}             {D}███     ███{R}",
)

@Composable
fun SplashScreen(onContinue: () -> Unit) {
    BoxWithConstraints(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .clickable { onContinue() },
    ) {
        // Fit the 71-char-wide, ART.size-line-tall art to the screen: monospace glyph
        // width ≈ 0.62 em, line height ≈ 1.35 em. In landscape the width bound alone
        // would blow past the (much shorter) available height, so cap by both axes —
        // whichever is tighter wins.
        val fontSize = with(LocalDensity.current) {
            val byWidth = constraints.maxWidth / 71f / 0.62f
            val footerPx = 24.dp.toPx() + 19.dp.toPx() // spacer + "tap anywhere" line
            val byHeight = (constraints.maxHeight - footerPx) / ART.size / 1.35f
            minOf(byWidth, byHeight).coerceAtLeast(6.dp.toPx()).toSp()
        }
        Column(
            modifier = Modifier.fillMaxSize(),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            ART.forEach { line -> TermText(tagged(line), fontSize = fontSize) }
            Spacer(Modifier.height(24.dp))
            TermText("— tap anywhere —", color = Term.dim)
        }
    }
}
