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
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.milo.jobfinder.AppViewModel
import dev.milo.jobfinder.data.Reply
import dev.milo.jobfinder.data.ReplyStatus

/**
 * What the companies said back. Invitations first — that's the thing worth
 * opening the app for — then anything the classifier wasn't sure about, then the
 * settled outcomes.
 */
@Composable
fun RepliesScreen(vm: AppViewModel, onOpenUrl: (String) -> Unit) {
    val replies = vm.replies
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Term.bg)
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TermText("✉ Replies", bold = true)
            WordButton("scan now", color = Term.cyan) { vm.scanInboxNow() }
        }
        vm.inboxStatus?.let {
            Spacer(Modifier.height(6.dp))
            TermText(it, color = Term.dim, fontSize = 11.sp)
        }
        Spacer(Modifier.height(20.dp))

        if (replies.isEmpty()) {
            TermText(
                "Nothing yet. The inbox is scanned hourly in the background,",
                color = Term.dim, fontSize = 12.sp,
            )
            TermText(
                "and replies to jobs you marked applied show up here.",
                color = Term.dim, fontSize = 12.sp,
            )
            return@Column
        }

        val invites = replies.filter { it.isInvite }
        val todo = replies.filter { it.needsAction }
        val unsure = replies.filter { !it.isActionable && it.needsReview }
        val settled = replies.filter { !it.isActionable && !it.needsReview }

        Section("Invitations", invites, Term.green, onOpenUrl)
        Section("Waiting on you", todo, Term.magenta, onOpenUrl)
        Section("Not sure — worth a look", unsure, Term.yellow, onOpenUrl)
        Section("Closed", settled, Term.dim, onOpenUrl)
    }
}

@Composable
private fun Section(
    title: String,
    replies: List<Reply>,
    color: Color,
    onOpenUrl: (String) -> Unit,
) {
    if (replies.isEmpty()) return
    TermText("── $title (${replies.size})", color = color, bold = true, fontSize = 12.sp)
    Spacer(Modifier.height(8.dp))
    replies.forEach { ReplyRow(it, onOpenUrl) }
    Spacer(Modifier.height(24.dp))
}

@Composable
private fun ReplyRow(reply: Reply, onOpenUrl: (String) -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onOpenUrl(reply.threadUrl) }
            .padding(vertical = 8.dp),
    ) {
        Row {
            TermText(badge(reply.status), color = statusColor(reply.status), fontSize = 12.sp)
            TermText(" ${reply.label}", bold = true, fontSize = 13.sp)
        }
        TermText(reply.subject, color = Term.fg, fontSize = 12.sp)
        val meta = buildString {
            append(reply.ts)
            append(" · ")
            append(reply.from)
            if (reply.key.isBlank()) append(" · unmatched")
            if (reply.classifiedBy == "heuristic") append(" · unverified")
        }
        TermText(meta, color = Term.dim, fontSize = 10.sp)
    }
}

private fun badge(status: String): String = when (status) {
    ReplyStatus.INVITED -> "[invite]"
    ReplyStatus.ACTION_NEEDED -> "[todo]"
    ReplyStatus.REJECTED -> "[no]"
    ReplyStatus.ACKNOWLEDGED -> "[ack]"
    else -> "[·]"
}

private fun statusColor(status: String): Color = when (status) {
    ReplyStatus.INVITED -> Term.green
    ReplyStatus.ACTION_NEEDED -> Term.magenta
    ReplyStatus.REJECTED -> Term.red
    ReplyStatus.ACKNOWLEDGED -> Term.cyan
    else -> Term.dim
}
