package com.powerk.app

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import kotlinx.coroutines.launch

fun Modifier.semanticsLabel(label: String): Modifier = semantics { contentDescription = label }

@Composable
fun ErrorBanner(message: String) {
    Surface(color = MaterialTheme.colorScheme.errorContainer, modifier = Modifier.fillMaxWidth()) {
        Text(message, Modifier.padding(12.dp), style = MaterialTheme.typography.bodySmall)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun AlarmChips(alarms: List<String>) {
    if (alarms.isNotEmpty()) FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        alarms.forEach { code ->
            SuggestionChip(onClick = {}, label = { Text(stringResource(alarmString(code))) },
                colors = SuggestionChipDefaults.suggestionChipColors(containerColor = MaterialTheme.colorScheme.errorContainer))
        }
    }
}

@Composable
fun RenameDialog(strip: Strip, outlet: Int, onDismiss: () -> Unit, onSaved: () -> Unit) {
    var name by remember { mutableStateOf(if (outlet == 0) strip.label else strip.outlet(outlet)?.name.orEmpty()) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val resources = LocalResources.current
    AlertDialog(onDismissRequest = { if (!busy) onDismiss() }, title = { Text(stringResource(R.string.rename)) }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(stringResource(R.string.name_hint))
            OutlinedTextField(name, { name = it.take(40) }, label = { Text(stringResource(R.string.field_name)) }, singleLine = true)
            error?.let { ErrorBanner(it) }
        }
    }, confirmButton = {
        TextButton(enabled = !busy, onClick = {
            busy = true
            scope.launch {
                try { Api.rename(Server.base, Server.token, strip.mac, outlet, name); onSaved(); onDismiss() }
                catch (e: Exception) { error = uiError(resources, e) }
                finally { busy = false }
            }
        }) { Text(stringResource(R.string.save)) }
    }, dismissButton = { TextButton(enabled = !busy, onClick = onDismiss) { Text(stringResource(R.string.cancel)) } })
}

@Composable
fun StripPicker(strips: List<Strip>, mac: String, onSelect: (String) -> Unit) {
    if (strips.size > 1) {
        Text(stringResource(R.string.choose_strip), style = MaterialTheme.typography.titleSmall)
        strips.forEach { strip ->
            Row(Modifier.fillMaxWidth(), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                RadioButton(selected = strip.mac == mac, onClick = { onSelect(strip.mac) })
                Text(markerText(strip.displayName), Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                Text(strip.mac.takeLast(6), style = LocalPowerkType.current.figures)
            }
        }
    }
}

@Composable
fun Field(value: String, onValueChange: (String) -> Unit, label: Int,
    numeric: Boolean = false, secret: Boolean = false) {
     OutlinedTextField(value, onValueChange, label = { Text(stringResource(label)) },
         textStyle = if (numeric || label == R.string.schedule_time || label == R.string.telegram_chat || label == R.string.ntfy_url)
             LocalPowerkType.current.figures else LocalPowerkType.current.body,
        singleLine = true, modifier = Modifier.fillMaxWidth(),
        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = when {
            secret -> androidx.compose.ui.text.input.KeyboardType.Password
            numeric -> androidx.compose.ui.text.input.KeyboardType.Decimal
            else -> androidx.compose.ui.text.input.KeyboardType.Text
        }), visualTransformation = if (secret) androidx.compose.ui.text.input.PasswordVisualTransformation()
        else androidx.compose.ui.text.input.VisualTransformation.None)
}
