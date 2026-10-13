package com.powerk.app

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.launch
import org.json.JSONObject

@Composable
fun RulesScreen(snapshot: Snapshot?) {
    var rules by remember { mutableStateOf<List<Rule>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf<Rule?>(null) }
    var deleting by remember { mutableStateOf<Rule?>(null) }
    val resources = LocalResources.current
    val scope = rememberCoroutineScope()
    suspend fun load() {
        if (!Server.configured) { error = resources.getString(R.string.no_server_title); return }
        val connection = Server.connection
        try {
            val result = connectionResult(connection, { Server.connection }) { Api.rules(it.base, it.token) }
            if (result != null) { rules = result; error = null }
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            if (connection.isCurrent(Server.connection)) error = uiError(resources, e)
        }
    }
    LaunchedEffect(Server.connection) { rules = emptyList(); error = null; load() }
    fun change(action: suspend () -> Unit) {
        busy = true
        scope.launch {
            try { action(); load() } catch (e: Exception) { error = uiError(resources, e) }
            finally { busy = false }
        }
    }
    Box(Modifier.fillMaxSize()) {
        LazyColumn(contentPadding = PaddingValues(18.dp, 16.dp, 18.dp, 96.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item {
                HighlightTitle(stringResource(R.string.tab_rules))
                Text(stringResource(R.string.rules_body), style = MaterialTheme.typography.bodyMedium)
                TextButton(onClick = { scope.launch { load() } }, enabled = !busy) { Text(stringResource(R.string.refresh)) }
                error?.let { ErrorBanner(it) }
            }
            if (rules.isEmpty() && error == null) item { Text(stringResource(R.string.no_rules)) }
            rules.groupBy { it.mac }.forEach { (mac, group) ->
                val strip = snapshot?.strips?.find { it.mac == mac }
                item { Text(markerText(strip?.displayName ?: mac), style = MaterialTheme.typography.titleLarge) }
                items(group, key = { it.id }) { rule ->
                    PaperCard(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(markerText(rule.label.ifBlank { resources.getString(kindString(rule.kind)) }), Modifier.weight(1f),
                                    style = MaterialTheme.typography.titleMedium)
                                Switch(rule.enabled, enabled = !busy, onCheckedChange = { enabled ->
                                    change { Api.updateRule(Server.base, Server.token, rule.id, JSONObject().put("enabled", enabled)) }
                                 }, colors = paperSwitchColors(), modifier = Modifier.semanticsLabel(resources.getString(R.string.rule_enabled)))
                            }
                            Text(resources.describe(rule, strip, snapshot?.utcOffsetSeconds ?: 0))
                            DashedDivider()
                            rule.nextAt?.let { Text(stringResource(R.string.next_action,
                                serverLocalTime(it, snapshot?.utcOffsetSeconds ?: 0)), style = MaterialTheme.typography.bodySmall) }
                            Row {
                                IconButton(enabled = !busy, onClick = { selected = rule; editing = true }) {
                                    Icon(Icons.Filled.Edit, stringResource(R.string.edit_rule))
                                }
                                IconButton(enabled = !busy, onClick = { deleting = rule }) {
                                    Icon(Icons.Filled.Delete, stringResource(R.string.delete_rule))
                                }
                            }
                        }
                    }
                }
            }
        }
        FloatingActionButton(onClick = { selected = null; editing = true },
            modifier = Modifier.align(Alignment.BottomEnd).padding(20.dp)) {
            Icon(Icons.Filled.Add, stringResource(R.string.add_rule))
        }
    }
    if (editing) RuleEditor(snapshot?.strips.orEmpty(), selected,
        onDismiss = { editing = false }, onSaved = { scope.launch { load() } })
    deleting?.let { rule ->
        AlertDialog(onDismissRequest = { deleting = null }, title = { Text(stringResource(R.string.delete_rule)) },
            text = { Text(stringResource(R.string.delete_rule_body)) }, confirmButton = {
                TextButton(onClick = { deleting = null; change { Api.deleteRule(Server.base, Server.token, rule.id) } }) {
                    Text(stringResource(R.string.delete_rule))
                }
            }, dismissButton = { TextButton(onClick = { deleting = null }) { Text(stringResource(R.string.cancel)) } })
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun RuleEditor(strips: List<Strip>, existing: Rule?, onDismiss: () -> Unit, onSaved: () -> Unit) {
    var kind by remember { mutableStateOf(existing?.kind ?: "schedule") }
    var mac by remember { mutableStateOf(existing?.mac ?: strips.firstOrNull()?.mac.orEmpty()) }
    var outlets by remember { mutableStateOf(existing?.outlets?.let { if (0 in it) (1..4).toSet() else it.toSet() } ?: setOf(1)) }
    var label by remember { mutableStateOf(existing?.label.orEmpty()) }
    var at by remember { mutableStateOf(existing?.at?.ifBlank { "07:30" } ?: "07:30") }
    var days by remember { mutableStateOf(existing?.days?.toSet() ?: (0..6).toSet()) }
    var on by remember { mutableStateOf(existing?.on ?: true) }
    var minutes by remember { mutableStateOf("60") }
    var onMin by remember { mutableStateOf((existing?.onMin ?: 10).toString()) }
    var offMin by remember { mutableStateOf((existing?.offMin ?: 50).toString()) }
    var belowW by remember { mutableStateOf((existing?.belowW ?: 3.0).toString()) }
    var forMin by remember { mutableStateOf((existing?.forMin ?: 10).toString()) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val resources = LocalResources.current
    val scope = rememberCoroutineScope()
    val draft = RuleDraft(kind, mac, outlets, label, at, days, on, minutes, onMin, offMin, belowW, forMin)
    LaunchedEffect(strips.map { it.mac }) {
        if (existing == null) mac = newRuleMac(mac, strips.map { it.mac })
    }
    Dialog(onDismissRequest = { if (!busy) onDismiss() }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize().safeDrawingPadding().imePadding(), color = MaterialTheme.colorScheme.background) {
            Column(Modifier.verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                HighlightTitle(stringResource(if (existing == null) R.string.add_rule else R.string.edit_rule))
                Text(stringResource(R.string.kind_label), style = MaterialTheme.typography.titleSmall)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ruleKinds.forEach { value ->
                        FilterChip(selected = kind == value, enabled = existing == null && !busy,
                            onClick = { kind = value }, label = { Text(stringResource(kindString(value))) })
                    }
                }
                StripPicker(strips, mac) { mac = it }
                Text(stringResource(R.string.choose_outlets), style = MaterialTheme.typography.titleSmall)
                (1..4).forEach { n ->
                    val outlet = strips.find { it.mac == mac }?.outlet(n)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(n in outlets, onCheckedChange = { outlets = if (it) outlets + n else outlets - n })
                        Text(stringResource(R.string.outlet_choice, n, outlet?.displayName(resources) ?: resources.getString(R.string.outlet_n, n)))
                    }
                }
                Field(label, { label = it.take(40) }, R.string.rule_label)
                when (kind) {
                    "schedule" -> {
                        Field(at, { at = it }, R.string.schedule_time)
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                            weekdayStrings.forEachIndexed { index, res ->
                                FilterChip(index in days, onClick = { days = if (index in days) days - index else days + index }, label = { Text(stringResource(res)) })
                            }
                        }
                    }
                    "timer" -> {
                        Field(minutes, { minutes = it }, R.string.timer_minutes, numeric = true)
                        if (existing != null) Text(stringResource(R.string.timer_reset_hint))
                    }
                    "cycle" -> {
                        Field(onMin, { onMin = it }, R.string.cycle_on, numeric = true)
                        Field(offMin, { offMin = it }, R.string.cycle_off, numeric = true)
                    }
                    "autooff" -> {
                        Field(belowW, { belowW = it }, R.string.autooff_watts, numeric = true)
                        Field(forMin, { forMin = it }, R.string.autooff_minutes, numeric = true)
                    }
                }
                if (kind == "schedule" || kind == "timer") Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(stringResource(R.string.action_on), Modifier.weight(1f))
                    Switch(on, { on = it }, colors = paperSwitchColors())
                }
                if (!draft.valid()) Text(stringResource(R.string.rule_validation), color = MaterialTheme.colorScheme.error)
                error?.let { ErrorBanner(it) }
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    TextButton(enabled = !busy, onClick = onDismiss) { Text(stringResource(R.string.cancel)) }
                    Button(enabled = !busy && draft.valid(), onClick = {
                        busy = true
                        scope.launch {
                            try {
                                if (existing == null) Api.createRule(Server.base, Server.token, draft.body())
                                else Api.updateRule(Server.base, Server.token, existing.id, draft.body().apply { remove("kind") })
                                onSaved(); onDismiss()
                            } catch (e: Exception) { error = uiError(resources, e) }
                            finally { busy = false }
                        }
                    }) { Text(stringResource(R.string.save)) }
                }
            }
        }
    }
}
