package com.powerk.app

import android.appwidget.AppWidgetManager
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

class WidgetConfigActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Lang.applyStored(this)
        setResult(RESULT_CANCELED)
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        if (id == AppWidgetManager.INVALID_APPWIDGET_ID) { finish(); return }
        setContent {
            PowerkTheme {
                var snapshot by remember { mutableStateOf<Snapshot?>(null) }
                var error by remember { mutableStateOf<String?>(null) }
                var loading by remember { mutableStateOf(true) }
                var saving by remember { mutableStateOf(false) }
                LaunchedEffect(Unit) {
                    try { snapshot = OutletControl.fresh() }
                    catch (e: Exception) { error = if (!Server.configured) getString(R.string.no_server_body) else uiError(this@WidgetConfigActivity, e) }
                    finally { loading = false }
                }
                Surface(Modifier.fillMaxSize()) {
                    Column(Modifier.safeDrawingPadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                        HighlightTitle(stringResource(R.string.widget_title))
                        if (loading || saving) LinearProgressIndicator(Modifier.fillMaxWidth())
                        error?.let { ErrorBanner(it) }
                        if (!Server.configured) Button(onClick = { startActivity(Intent(this@WidgetConfigActivity, MainActivity::class.java)); finish() }) {
                            Text(stringResource(R.string.open_settings))
                        }
                        if (!saving) TargetList(snapshot?.strips.orEmpty()) { target ->
                            saving = true
                            TargetPrefs.setWidget(this@WidgetConfigActivity, id, target)
                            lifecycleScope.launch {
                                OutletWidget.update(this@WidgetConfigActivity, id)
                                setResult(RESULT_OK, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id))
                                finish()
                            }
                        }
                        TextButton(onClick = { finish() }) { Text(stringResource(R.string.cancel)) }
                    }
                }
            }
        }
    }
}
