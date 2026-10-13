package com.powerk.app

import android.content.ComponentName
import android.service.quicksettings.TileService
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

@Composable
fun ServerSettingsSection(fixture: ServerSettings? = null) {
    var settings by remember { mutableStateOf<ServerSettings?>(null) }
    var settingsConnection by remember { mutableStateOf<ServerConnection?>(null) }
    var currency by remember { mutableStateOf("") }
    var price by remember { mutableStateOf("") }
    var budget by remember { mutableStateOf("") }
    var temp by remember { mutableStateOf("") }
    var power by remember { mutableStateOf("") }
    var ntfy by remember { mutableStateOf("") }
    var chat by remember { mutableStateOf("") }
    var token by remember { mutableStateOf("") }
    var clearToken by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var status by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val resources = LocalResources.current
    val scope = rememberCoroutineScope()
    fun bind(s: ServerSettings) {
        settingsConnection = Server.connection
        settings = s; currency = s.currency; price = s.priceKwh.toString(); budget = s.budget?.toString().orEmpty()
        temp = s.tempMaxC.toString(); power = s.powerMaxW.toString(); ntfy = s.ntfyUrl; chat = s.telegramChat
        token = ""; clearToken = false
    }
    suspend fun load() {
        if (!Server.configured) return
        val connection = Server.connection
        busy = true
        try {
            val result = connectionResult(connection, { Server.connection }) { Api.settings(it.base, it.token) }
            if (result != null) { bind(result); error = null }
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            if (connection.isCurrent(Server.connection)) error = uiError(resources, e)
        } finally { if (connection.isCurrent(Server.connection)) busy = false }
    }
    LaunchedEffect(Server.connection) {
        settings = null; error = null; status = null; busy = false
        if (fixture != null) bind(fixture) else load()
    }
    PaperCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Text(markerText(stringResource(R.string.server_settings)), style = MaterialTheme.typography.titleMedium)
            TextButton(enabled = !busy && Server.configured, onClick = { scope.launch { load() } }) { Text(stringResource(R.string.refresh)) }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            error?.let { ErrorBanner(it) }
            if (settings != null && settingsConnection == Server.connection) {
                Field(currency, { currency = it.take(8) }, R.string.currency)
                Field(price, { price = it }, R.string.price_kwh, numeric = true)
                Field(budget, { budget = it }, R.string.monthly_budget, numeric = true)
                Text(stringResource(R.string.budget_hint), style = MaterialTheme.typography.bodySmall)
                Field(temp, { temp = it }, R.string.max_temp, numeric = true)
                Field(power, { power = it }, R.string.max_power, numeric = true)
                Field(ntfy, { ntfy = it }, R.string.ntfy_url)
                Field(chat, { chat = it }, R.string.telegram_chat)
                Field(token, { token = it; clearToken = false }, R.string.telegram_token, secret = true)
                Text(stringResource(when {
                    clearToken -> R.string.token_clear_hint
                    settings!!.telegramConfigured -> R.string.token_saved_hint
                    else -> R.string.token_keep_hint
                }), style = MaterialTheme.typography.bodySmall)
                TextButton(enabled = !busy, onClick = { token = ""; clearToken = true }) { Text(stringResource(R.string.clear_token)) }
                val priceValue = price.apiDouble()
                val budgetValue = budget.apiDouble()
                val tempValue = temp.apiDouble()
                val powerValue = power.apiDouble()
                val valid = currency.isNotBlank() && currency.length <= 8 &&
                    priceValue != null && priceValue.isFinite() && priceValue in 0.0..1000.0 &&
                    (budget.isBlank() || (budgetValue != null && budgetValue.isFinite() && budgetValue > 0)) &&
                    tempValue != null && tempValue in 30.0..120.0 && powerValue != null && powerValue in 100.0..10000.0 &&
                    (ntfy.isBlank() || Regex("https?://.+").matches(ntfy))
                if (!valid) Text(stringResource(R.string.settings_validation), color = MaterialTheme.colorScheme.error)
                Button(enabled = valid && !busy, onClick = {
                    busy = true; status = null
                    val connection = Server.connection
                    val body = settingsBody(ServerSettings(currency, priceValue!!, budgetValue, tempValue!!, powerValue!!, ntfy, chat), token, clearToken)
                    scope.launch {
                        try {
                            val saved = connectionResult(connection, { Server.connection }) { Api.saveSettings(it.base, it.token, body) }
                                ?: return@launch
                            bind(saved)
                            // Re-read the canonical server values after a change.
                            val updated = connectionResult(connection, { Server.connection }) { Api.settings(it.base, it.token) }
                                ?: return@launch
                            bind(updated); error = null
                            status = resources.getString(R.string.saved)
                        } catch (e: Exception) {
                            if (e is kotlinx.coroutines.CancellationException) throw e
                            if (connection.isCurrent(Server.connection)) error = uiError(resources, e)
                        } finally { if (connection.isCurrent(Server.connection)) busy = false }
                    }
                }, modifier = Modifier.fillMaxWidth()) { Text(stringResource(R.string.save)) }
                OutlinedButton(enabled = !busy, onClick = {
                    busy = true; status = null
                    val connection = Server.connection
                    scope.launch {
                        try {
                            val result = connectionResult(connection, { Server.connection }) { Api.testAlerts(it.base, it.token) }
                                ?: return@launch
                            status = if (!result.ok && result.sent.isEmpty() && result.failed.isEmpty())
                                resources.getString(R.string.no_push_channels)
                            else resources.getString(R.string.notification_result,
                                result.sent.joinToString(", ").ifBlank { resources.getString(R.string.none) },
                                result.failed.joinToString(", ") { "${it.channel}: ${it.error}" }.ifBlank { resources.getString(R.string.none) })
                            error = null
                        } catch (e: Exception) {
                            if (e is kotlinx.coroutines.CancellationException) throw e
                            if (connection.isCurrent(Server.connection)) error = uiError(resources, e)
                        } finally { if (connection.isCurrent(Server.connection)) busy = false }
                    }
                }, modifier = Modifier.fillMaxWidth()) { Text(stringResource(R.string.test_notification)) }
                status?.let { Text(it) }
            }
        }
    }
}

@Composable
fun TileTargetSection(snapshot: Snapshot?) {
    val context = LocalContext.current
    var target by remember { mutableStateOf(TargetPrefs.tile(context)) }
    var choosing by remember { mutableStateOf(false) }
    PaperCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(markerText(stringResource(R.string.tile_title)), style = MaterialTheme.typography.titleMedium)
            Text(target?.let { "${it.mac} · ${it.name}" } ?: stringResource(R.string.target_not_set))
            Button(onClick = { choosing = true }) { Text(stringResource(R.string.choose_outlet)) }
            if (target != null) TextButton(onClick = {
                TargetPrefs.setTile(context, null); target = null
                TileService.requestListeningState(context, ComponentName(context, OutletTileService::class.java))
            }) { Text(stringResource(R.string.clear_target)) }
        }
    }
    if (choosing) AlertDialog(onDismissRequest = { choosing = false }, title = { Text(stringResource(R.string.choose_outlet)) },
        text = { TargetList(snapshot?.strips.orEmpty()) {
            TargetPrefs.setTile(context, it); target = it; choosing = false
            TileService.requestListeningState(context, ComponentName(context, OutletTileService::class.java))
        } }, confirmButton = { TextButton(onClick = { choosing = false }) { Text(stringResource(R.string.cancel)) } })
}
