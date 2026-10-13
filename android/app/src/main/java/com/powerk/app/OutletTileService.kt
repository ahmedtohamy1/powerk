package com.powerk.app

import android.app.PendingIntent
import android.annotation.SuppressLint
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

class OutletTileService : TileService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var refreshJob: Job? = null
    private var busy = false
    override fun onStartListening() {
        super.onStartListening()
        if (busy) return
        refreshJob?.cancel()
        refreshJob = scope.launch {
            val snapshot = try { OutletControl.fresh() } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                null
            }
            render(snapshot)
        }
    }
    override fun onStopListening() {
        refreshJob?.cancel()
        super.onStopListening()
    }
    override fun onClick() {
        super.onClick()
        if (TargetPrefs.tile(this) == null) { openApp(); return }
        if (isLocked) { unlockAndRun { toggle() }; return }
        toggle()
    }
    private fun toggle() {
        val target = TargetPrefs.tile(this) ?: return
        if (busy) return
        busy = true
        refreshJob?.cancel()
        scope.launch {
            try {
                val result = OutletControl.toggle(target)
                if (result != null) render(result.snapshot, if (result.confirmed) null else localized().getString(R.string.not_confirmed))
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                render(null, localized().getString(R.string.not_confirmed))
            }
            finally { busy = false }
        }
    }
    private fun render(snapshot: Snapshot?, message: String? = null) {
        val tile = qsTile ?: return
        val local = localized()
        val target = TargetPrefs.tile(this)
        val resolved = target?.let { snapshot?.resolve(it) }
        tile.label = resolved?.second?.displayName(local) ?: target?.name ?: local.getString(R.string.tile_title)
        tile.state = when {
            target == null -> Tile.STATE_INACTIVE // Keep an unconfigured tile tappable to open the app.
            resolved?.first?.online != true -> Tile.STATE_UNAVAILABLE
            resolved.second.on -> Tile.STATE_ACTIVE
            else -> Tile.STATE_INACTIVE
        }
        val status = message ?: local.getString(when {
            target == null -> R.string.target_not_set
            resolved?.first?.online != true -> R.string.state_offline
            resolved.second.on -> R.string.state_on
            else -> R.string.state_off
        })
        if (Build.VERSION.SDK_INT >= 29) tile.subtitle = status
        tile.contentDescription = "${tile.label} · $status"
        tile.updateTile()
    }
    @Suppress("DEPRECATION")
    @SuppressLint("StartActivityAndCollapseDeprecated") // Guarded pre-34 fallback; PendingIntent overload is API 34+.
    private fun openApp() {
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
        else startActivityAndCollapse(intent)
    }
    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }
}
