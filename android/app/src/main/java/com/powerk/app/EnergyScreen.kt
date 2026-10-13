package com.powerk.app

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

@Composable
fun EnergyScreen(snapshot: Snapshot?) {
    val strips = snapshot?.strips.orEmpty()
    var mac by remember { mutableStateOf(strips.firstOrNull()?.mac.orEmpty()) }
    var range by remember { mutableStateOf("day") }
    var history by remember { mutableStateOf<History?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    val resources = LocalResources.current
    val scope = rememberCoroutineScope()
    suspend fun load() {
        if (mac.isBlank()) return
        val connection = Server.connection
        val requestedMac = mac
        val requestedRange = range
        loading = true
        fun current() = connection.isCurrent(Server.connection) && mac == requestedMac && range == requestedRange
        try {
            val result = Api.history(connection.base, connection.token, requestedMac, requestedRange)
            if (current()) { history = result; error = null }
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            if (current()) error = uiError(resources, e)
        } finally { if (current()) loading = false }
    }
    LaunchedEffect(strips.map { it.mac }) { if (strips.none { it.mac == mac }) mac = strips.firstOrNull()?.mac.orEmpty() }
    LaunchedEffect(mac, range, Server.connection) { history = null; error = null; loading = false; load() }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)) {
        HighlightTitle(stringResource(R.string.tab_energy))
        Text(stringResource(R.string.energy_estimate), style = MaterialTheme.typography.bodyMedium)
        if (strips.isEmpty()) Text(stringResource(R.string.no_strip_title))
        StripPicker(strips, mac) { mac = it }
        val ranges = listOf("day", "week", "month", "year")
        val labels = listOf(R.string.range_day, R.string.range_week, R.string.range_month, R.string.range_year)
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
            ranges.forEachIndexed { index, value ->
                SegmentedButton(selected = range == value, onClick = { range = value },
                    shape = Wobble2, colors = paperSegmentColors()) { Text(stringResource(labels[index])) }
            }
        }
        TextButton(enabled = !loading, onClick = { scope.launch { load() } }) { Text(stringResource(R.string.refresh)) }
        if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
        error?.let { ErrorBanner(it) }
        history?.let { h ->
            Text(stringResource(R.string.energy_total, h.totalKwh.toString(), h.totalCost.toString(), h.currency),
                style = LocalPowerkType.current.figures)
            val barColor = LocalPowerkColors.current.chart
            val baseline = MaterialTheme.colorScheme.outlineVariant
            Canvas(Modifier.fillMaxWidth().height(180.dp).semanticsLabel(resources.getString(R.string.energy_chart))) {
                val max = h.buckets.maxOfOrNull { it.kwh }?.coerceAtLeast(0.001) ?: 1.0
                val width = size.width / h.buckets.size.coerceAtLeast(1)
                drawLine(baseline, Offset(0f, size.height), Offset(size.width, size.height))
                h.buckets.forEachIndexed { index, bucket ->
                    val height = (bucket.kwh / max).toFloat() * size.height
                    drawRect(barColor, Offset(index * width + width * 0.12f, size.height - height), Size(width * 0.76f, height))
                }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                h.buckets.firstOrNull()?.let { Text(serverLocalTime(it.t, snapshot?.utcOffsetSeconds ?: 0,
                    if (range == "day") "HH:mm" else if (range == "year") "MMM yyyy" else "dd MMM"), style = MaterialTheme.typography.labelSmall) }
                h.buckets.lastOrNull()?.let { Text(serverLocalTime(it.t, snapshot?.utcOffsetSeconds ?: 0,
                    if (range == "day") "HH:mm" else if (range == "year") "MMM yyyy" else "dd MMM"), style = MaterialTheme.typography.labelSmall) }
            }
            Text(stringResource(R.string.outlet_breakdown), style = MaterialTheme.typography.titleMedium)
            h.perOutlet.forEach { energy ->
                DashedDivider()
                val outlet = strips.find { it.mac == mac }?.outlet(energy.n)
                Row(Modifier.fillMaxWidth()) {
                    Text(outlet?.displayName(resources) ?: resources.getString(R.string.outlet_n, energy.n), Modifier.weight(1f))
                    Text(stringResource(R.string.energy_kwh, energy.kwh.toString()), style = LocalPowerkType.current.figures)
                }
            }
        }
        snapshot?.let { BudgetRing(it) }
    }
}

@Composable
private fun BudgetRing(snapshot: Snapshot) {
    Text(stringResource(R.string.monthly_budget), style = MaterialTheme.typography.titleMedium)
    val pct = budgetPercentage(snapshot.monthCost, snapshot.budget)
    if (pct == null) { Text(stringResource(R.string.budget_off)); return }
    val scheme = MaterialTheme.colorScheme
    // A dedicated amber warning role is shared by the ring and warning events.
    val color = when { pct >= 90 -> scheme.error; pct >= 75 -> warningColor(); else -> LocalPowerkColors.current.ok }
    val label = stringResource(R.string.budget_usage, pct.toInt(), snapshot.monthCost.toString(), snapshot.budget.toString(), snapshot.currency)
    Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        Box(Modifier.size(180.dp), contentAlignment = Alignment.Center) {
            Canvas(Modifier.fillMaxSize().semanticsLabel(label)) {
                val stroke = 12.dp.toPx()
                val arcSize = Size(size.width - stroke, size.height - stroke)
                val top = Offset(stroke / 2, stroke / 2)
                drawArc(scheme.surfaceContainerHighest, -90f, 360f, false, top, arcSize, style = Stroke(stroke, cap = StrokeCap.Round))
                drawArc(color, -90f, (pct.coerceAtMost(100.0) * 3.6).toFloat(), false, top, arcSize, style = Stroke(stroke, cap = StrokeCap.Round))
            }
            Text(stringResource(R.string.percent_value, pct.toInt()), style = LocalPowerkType.current.figures, color = color)
        }
    }
    Text(label)
}
