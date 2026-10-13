package com.powerk.app

import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.sync.Mutex

data class OutletTarget(val mac: String, val n: Int, val name: String)

object TargetPrefs {
    private fun prefs(context: Context) = context.applicationContext.getSharedPreferences("targets", Context.MODE_PRIVATE)
    private fun read(context: Context, key: String): OutletTarget? {
        val p = prefs(context)
        val mac = p.getString("${key}_mac", "").orEmpty()
        val n = p.getInt("${key}_n", 0)
        return if (mac.isBlank() || n !in 1..4) null else OutletTarget(mac, n, p.getString("${key}_name", "").orEmpty())
    }
    private fun write(context: Context, key: String, target: OutletTarget?) {
        prefs(context).edit().apply {
            if (target == null) { remove("${key}_mac"); remove("${key}_n"); remove("${key}_name") }
            else { putString("${key}_mac", target.mac); putInt("${key}_n", target.n); putString("${key}_name", target.name) }
        }.apply()
    }
    fun tile(context: Context) = read(context, "tile")
    fun setTile(context: Context, target: OutletTarget?) = write(context, "tile", target)
    fun widget(context: Context, id: Int) = read(context, "widget_$id")
    fun setWidget(context: Context, id: Int, target: OutletTarget?) = write(context, "widget_$id", target)
}

/** Pure identity resolution, including reordered devices and outlets. */
fun Snapshot.resolve(target: OutletTarget): Pair<Strip, Outlet>? {
    val strip = strips.find { it.mac == target.mac } ?: return null
    val outlet = strip.outlet(target.n) ?: return null
    return strip to outlet
}

data class ToggleOutcome(val snapshot: Snapshot, val confirmed: Boolean)

/** Exactly one fresh read before sending an explicit state; no command retry, even when unconfirmed. */
internal suspend fun toggleFresh(target: OutletTarget, fetch: suspend () -> Snapshot,
    send: suspend (String, Int, Boolean) -> Boolean): ToggleOutcome {
    val fresh = fetch()
    val (strip, outlet) = fresh.resolve(target) ?: throw ServerException("", messageRes = R.string.outlet_unavailable)
    if (!strip.online) throw ServerException("", 503, R.string.state_offline)
    val confirmed = send(target.mac, target.n, !outlet.on)
    return ToggleOutcome(fetch(), confirmed)
}

object OutletControl {
    // Prevent overlapping taps from undoing a just-issued command in this process.
    private val commandLock = Mutex()
    suspend fun fresh(): Snapshot {
        if (!Server.configured) throw ServerException("", messageRes = R.string.no_server_title)
        val connection = Server.connection
        return connectionResult(connection, { Server.connection }) { Api.snapshot(it.base, it.token) }
            ?: throw ServerException("", messageRes = R.string.connection_changed)
    }
    suspend fun toggle(target: OutletTarget): ToggleOutcome? {
        if (!commandLock.tryLock()) return null
        try {
            // Capture connection settings for the whole operation if Settings changes concurrently.
            val connection = Server.connection
            return toggleFresh(target, {
                connectionResult(connection, { Server.connection }) { Api.snapshot(it.base, it.token) }
                    ?: throw ServerException("", messageRes = R.string.connection_changed)
            }) { mac, n, on ->
                if (!connection.isCurrent(Server.connection)) throw ServerException("", messageRes = R.string.connection_changed)
                Api.setOutlet(connection.base, connection.token, mac, n, on)
            }
        } finally { commandLock.unlock() }
    }
}

@Composable
fun TargetList(strips: List<Strip>, onChoose: (OutletTarget) -> Unit) {
    val context = LocalContext.current
    Column(Modifier.fillMaxWidth().heightIn(max = 480.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (strips.isEmpty()) Text(stringResource(R.string.no_strip_title))
        strips.forEach { strip ->
            Text(markerText(strip.displayName), style = MaterialTheme.typography.titleMedium)
            Text(strip.mac, style = LocalPowerkType.current.figures)
            strip.outlets.sortedBy { it.n }.filter { it.n in 1..4 }.forEach { outlet ->
                OutlinedButton(onClick = { onChoose(OutletTarget(strip.mac, outlet.n, outlet.displayName(context))) }, modifier = Modifier.fillMaxWidth()) {
                    Text(stringResource(R.string.outlet_choice, outlet.n, outlet.displayName(context)))
                }
            }
        }
    }
}
