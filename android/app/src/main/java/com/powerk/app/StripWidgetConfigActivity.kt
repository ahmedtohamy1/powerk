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

class StripWidgetConfigActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Lang.applyStored(this)
        setResult(RESULT_CANCELED)
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        if (id == AppWidgetManager.INVALID_APPWIDGET_ID) { finish(); return }
        fun choose(mac: String) {
            StripWidgetPrefs.save(this, id, mac)
            WidgetUpdates.render(this, StripWidget::class.java, id)
            setResult(RESULT_OK, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id))
            finish()
        }
        setContent {
            PowerkTheme {
                var snapshot by remember { mutableStateOf<Snapshot?>(null) }
                var error by remember { mutableStateOf<String?>(null) }
                var loading by remember { mutableStateOf(true) }
                LaunchedEffect(Unit) {
                    try {
                        val connection = Server.connection
                        val fresh = OutletControl.fresh()
                        snapshot = fresh
                        WidgetUpdates.freshSnapshot(this@StripWidgetConfigActivity, fresh, connection)
                        if (fresh.strips.size == 1) choose(fresh.strips.single().mac)
                    } catch (e: Exception) { error = uiError(this@StripWidgetConfigActivity, e) }
                    finally { loading = false }
                }
                Surface(Modifier.fillMaxSize()) {
                    Column(Modifier.safeDrawingPadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                        HighlightTitle(stringResource(R.string.choose_strip))
                        if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                        error?.let { ErrorBanner(it) }
                        if (!loading && snapshot?.strips.orEmpty().isEmpty()) Text(stringResource(R.string.no_strip_title))
                        StripPicker(snapshot?.strips.orEmpty(), "") { choose(it) }
                        if (!Server.configured) Button(onClick = {
                            startActivity(Intent(this@StripWidgetConfigActivity, MainActivity::class.java)); finish()
                        }) { Text(stringResource(R.string.open_settings)) }
                        TextButton(onClick = { finish() }) { Text(stringResource(R.string.cancel)) }
                    }
                }
            }
        }
    }
}
