package dev.milo.jobfinder

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.core.content.ContextCompat
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import dev.milo.jobfinder.ui.ApplyScreen
import dev.milo.jobfinder.ui.DeckScreen
import dev.milo.jobfinder.ui.MenuScreen
import dev.milo.jobfinder.ui.RepliesScreen
import dev.milo.jobfinder.ui.RoleInputScreen
import dev.milo.jobfinder.ui.SearchScreen
import dev.milo.jobfinder.ui.SplashScreen
import dev.milo.jobfinder.ui.SummaryScreen
import dev.milo.jobfinder.ui.Term

class MainActivity : ComponentActivity() {

    private val vm: AppViewModel by viewModels()

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* either way */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        askForNotifications()
        handleShareIntent(intent)
        handleReplyIntent(intent)
        setContent {
            Surface(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Term.bg)
                    .systemBarsPadding(),
                color = Term.bg,
            ) {
                BackHandler(enabled = true) {
                    if (!vm.back()) finish()
                }
                when (vm.screen) {
                    Screen.Splash -> SplashScreen { vm.replace(Screen.Menu) }
                    Screen.Menu -> MenuScreen(vm)
                    Screen.RoleInput -> RoleInputScreen(vm)
                    Screen.Searching -> SearchScreen(vm)
                    Screen.Deck -> DeckScreen(
                        vm = vm,
                        onOpenUrl = ::openUrl,
                        onApply = { vm.openApply(it) },
                        onFinish = { vm.replace(Screen.Summary) },
                    )
                    Screen.Summary -> SummaryScreen(vm) { vm.openApply(it) }
                    Screen.Apply -> ApplyScreen(
                        vm = vm,
                        onOpenUrl = ::openUrl,
                        onCopy = ::copyToClipboard,
                        onShare = ::shareText,
                    )
                    Screen.Replies -> RepliesScreen(vm, onOpenUrl = ::openUrl)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
        handleReplyIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        // The hourly worker scans while the app is backgrounded; the in-memory
        // reply list is a snapshot, so re-read it whenever we come forward.
        vm.refreshReplies()
    }

    /** Tapping an interview-invite notification lands on the replies list. */
    private fun handleReplyIntent(intent: Intent?) {
        if (intent?.getBooleanExtra(Notifications.EXTRA_OPEN_REPLIES, false) == true) {
            vm.openReplies()
        }
    }

    private fun askForNotifications() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        ) return
        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun handleShareIntent(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND || intent.type != "text/plain") return
        val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
        val url = Regex("https?://\\S+").find(text)?.value
        if (url != null) {
            vm.openSharedUrl(url.trimEnd('.', ',', ')', '»'))
        } else {
            Toast.makeText(this, "No URL found in the shared text", Toast.LENGTH_SHORT).show()
        }
    }

    private fun openUrl(url: String) {
        if (url.isBlank()) return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: Exception) {
            Toast.makeText(this, "Can't open: $url", Toast.LENGTH_SHORT).show()
        }
    }

    private fun copyToClipboard(text: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("jobfinder", text))
        Toast.makeText(this, "Copied", Toast.LENGTH_SHORT).show()
    }

    private fun shareText(text: String) {
        startActivity(
            Intent.createChooser(
                Intent(Intent.ACTION_SEND).apply {
                    type = "text/plain"
                    putExtra(Intent.EXTRA_TEXT, text)
                },
                "Share",
            )
        )
    }
}
