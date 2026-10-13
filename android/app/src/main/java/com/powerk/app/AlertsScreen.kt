package com.powerk.app

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

@Composable
fun warningColor(): Color = LocalPowerkColors.current.warn

@Composable
fun AlertsScreen(snapshot: Snapshot?, onRead: () -> Unit) {
    var events by remember { mutableStateOf<List<Event>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var more by remember { mutableStateOf(false) }
    val resources = LocalResources.current
    val scope = rememberCoroutineScope()
    suspend fun load(older: Boolean = false) {
        if (busy) return
        if (!Server.configured) { error = resources.getString(R.string.no_server_title); return }
        val connection = Server.connection
        busy = true
        try {
            val page = connectionResult(connection, { Server.connection }) {
                Api.events(it.base, it.token, if (older) events.minOfOrNull { it.id } else null)
            } ?: return
            events = (if (older) events + page.events else page.events).distinctBy { it.id }.sortedByDescending { it.id }
            more = page.events.size == 50
            error = null
            if (!older) page.events.maxOfOrNull { it.id }?.let {
                Api.readEvents(connection.base, connection.token, it)
                if (connection.isCurrent(Server.connection)) onRead()
            }
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            if (connection.isCurrent(Server.connection)) error = uiError(resources, e)
        } finally { if (connection.isCurrent(Server.connection)) busy = false }
    }
    LaunchedEffect(Server.connection) { events = emptyList(); error = null; more = false; busy = false; load() }
    LazyColumn(contentPadding = PaddingValues(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            HighlightTitle(stringResource(R.string.tab_alerts))
            TextButton(enabled = !busy, onClick = { scope.launch { load() } }) { Text(stringResource(R.string.refresh)) }
            error?.let { ErrorBanner(it) }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (events.isEmpty() && !busy && error == null) Text(stringResource(R.string.no_alerts))
        }
        items(events, key = { it.id }) { event ->
            val scheme = MaterialTheme.colorScheme
            val color = when (event.level) { "alert" -> scheme.error; "warn" -> warningColor(); else -> LocalPowerkColors.current.ok }
            PaperCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(stringResource(when (event.level) { "alert" -> R.string.level_alert; "warn" -> R.string.level_warn; else -> R.string.level_info }),
                        color = color, style = MaterialTheme.typography.labelLarge)
                    val strip = snapshot?.strips?.find { it.mac == event.mac }
                    val target = listOfNotNull(strip?.displayName ?: event.mac.ifBlank { null },
                        if (event.outlet > 0) strip?.outlet(event.outlet)?.displayName(resources)
                            ?: resources.getString(R.string.outlet_n, event.outlet) else null).joinToString(" · ")
                    if (target.isNotBlank()) Text(markerText(target), style = MaterialTheme.typography.titleMedium)
                    Text(resources.eventText(event))
                    DashedDivider()
                    Text(serverLocalTime(event.ts, snapshot?.utcOffsetSeconds ?: 0), style = LocalPowerkType.current.figures)
                }
            }
        }
        if (more) item {
            OutlinedButton(enabled = !busy, onClick = { scope.launch { load(true) } }, modifier = Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.load_more))
            }
        }
    }
}
