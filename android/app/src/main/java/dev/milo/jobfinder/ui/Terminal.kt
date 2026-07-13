package dev.milo.jobfinder.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** Terminal palette, mapped from the TUI's ANSI colors. */
object Term {
    val bg = Color(0xFF0C0E0C)
    val fg = Color(0xFFDADADA)
    val dim = Color(0xFF7A837A)
    val green = Color(0xFF5AF78E)
    val red = Color(0xFFFF5C57)
    val cyan = Color(0xFF57C7FF)
    val yellow = Color(0xFFF3F99D)
    val magenta = Color(0xFFFF6AC1)
}

val termStyle = TextStyle(
    fontFamily = FontFamily.Monospace,
    fontSize = 14.sp,
    lineHeight = 19.sp,
    color = Term.fg,
)

@Composable
fun TermText(
    text: String,
    color: Color = Term.fg,
    modifier: Modifier = Modifier,
    bold: Boolean = false,
    fontSize: TextUnit = 14.sp,
) {
    Text(
        text = text,
        modifier = modifier,
        style = termStyle.copy(
            color = color,
            fontSize = fontSize,
            lineHeight = fontSize * 1.35f,
            fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal,
        ),
    )
}

@Composable
fun TermText(text: AnnotatedString, modifier: Modifier = Modifier, fontSize: TextUnit = 14.sp) {
    Text(
        text = text,
        modifier = modifier,
        style = termStyle.copy(fontSize = fontSize, lineHeight = fontSize * 1.35f),
    )
}

/** A TUI-style "button": an underlined word. */
@Composable
fun WordButton(
    label: String,
    color: Color = Term.fg,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    Text(
        text = label,
        style = termStyle.copy(
            color = if (enabled) color else Term.dim.copy(alpha = 0.4f),
            textDecoration = TextDecoration.Underline,
        ),
        modifier = Modifier
            .clickable(enabled = enabled) { onClick() }
            .padding(horizontal = 6.dp, vertical = 8.dp),
    )
}

fun stars(value: Int): AnnotatedString = buildAnnotatedString {
    if (value < 0) {
        withStyle(SpanStyle(color = Term.dim)) { append("n/a") }
        return@buildAnnotatedString
    }
    val n = value.coerceIn(0, 5)
    withStyle(SpanStyle(color = Term.yellow)) { append("★".repeat(n)) }
    withStyle(SpanStyle(color = Term.dim)) { append("☆".repeat(5 - n)) }
}

private fun AnnotatedString.Builder.withStyle(style: SpanStyle, block: AnnotatedString.Builder.() -> Unit) {
    val i = pushStyle(style)
    block()
    pop()
}

/**
 * Parse the TUI's {C}/{G}/{B}/{D}/{R} color-tag notation into an AnnotatedString,
 * so the splash art can be ported verbatim.
 */
fun tagged(line: String): AnnotatedString = buildAnnotatedString {
    var color: Color? = null
    var bold = false
    var i = 0
    fun style() = SpanStyle(
        color = color ?: Term.fg,
        fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal,
    )
    while (i < line.length) {
        if (line[i] == '{' && i + 2 < line.length && line[i + 2] == '}') {
            when (line[i + 1]) {
                'C' -> color = Term.cyan
                'G' -> color = Term.green
                'D' -> color = Term.dim
                'M' -> color = Term.magenta
                'Y' -> color = Term.yellow
                'E' -> color = Term.red
                'B' -> bold = true
                'R' -> { color = null; bold = false }
            }
            i += 3
        } else {
            val start = i
            i++ // consume at least one char (covers a literal '{')
            while (i < line.length && line[i] != '{') i++
            pushStyle(style())
            append(line.substring(start, i))
            pop()
        }
    }
}
