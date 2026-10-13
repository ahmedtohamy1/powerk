package com.powerk.app

import android.provider.Settings
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.shape.CornerBasedShape
import androidx.compose.foundation.shape.CornerSize
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.*
import androidx.compose.ui.graphics.*
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.*
import java.util.Locale

/** CSS scales every radius by one factor, not each corner independently. Order: TL, TR, BR, BL. */
data class EllipticalRadii(val x: List<Float>, val y: List<Float>) {
    fun scale(width: Float, height: Float): Float = minOf(1f,
        width / (x[0] + x[1]), width / (x[3] + x[2]), height / (y[0] + y[3]), height / (y[1] + y[2])).coerceAtLeast(0f)
}
class HandCutShape(private val alternate: Boolean = false,
    topStart: CornerSize = CornerSize(1.dp), topEnd: CornerSize = CornerSize(1.dp),
    bottomEnd: CornerSize = CornerSize(1.dp), bottomStart: CornerSize = CornerSize(1.dp)) : CornerBasedShape(topStart, topEnd, bottomEnd, bottomStart) {
    override fun createOutline(size: Size, topStart: Float, topEnd: Float, bottomEnd: Float,
        bottomStart: Float, layoutDirection: LayoutDirection): Outline {
        if (size.width <= 0f || size.height <= 0f || topStart == 0f) return Outline.Rectangle(Rect(Offset.Zero, size))
        // Carry density in a tiny corner: do not let CornerBasedShape pre-normalize circular radii.
        // The CSS elliptical normalization below is the only normalization of the actual radii.
        val unit = topStart
        val xRadii = (if (alternate) listOf(15f, 225f, 15f, 255f) else listOf(255f, 15f, 225f, 15f)).map { it * unit }
        val yRadii = (if (alternate) listOf(255f, 15f, 225f, 15f) else listOf(15f, 225f, 15f, 255f)).map { it * unit }
        val radii = EllipticalRadii(xRadii, yRadii)
        val f = radii.scale(size.width, size.height)
        val x = radii.x.map { it * f }; val y = radii.y.map { it * f }
        val w = size.width; val h = size.height
        val path = Path().apply {
            moveTo(x[0], 0f); lineTo(w - x[1], 0f)
            arcTo(Rect(w - 2 * x[1], 0f, w, 2 * y[1]), -90f, 90f, false)
            lineTo(w, h - y[2]); arcTo(Rect(w - 2 * x[2], h - 2 * y[2], w, h), 0f, 90f, false)
            lineTo(x[3], h); arcTo(Rect(0f, h - 2 * y[3], 2 * x[3], h), 90f, 90f, false)
            lineTo(0f, y[0]); arcTo(Rect(0f, 0f, 2 * x[0], 2 * y[0]), 180f, 90f, false); close()
        }
        return Outline.Generic(path)
    }
    override fun copy(topStart: CornerSize, topEnd: CornerSize, bottomEnd: CornerSize, bottomStart: CornerSize): CornerBasedShape =
        HandCutShape(alternate, topStart, topEnd, bottomEnd, bottomStart)
}
val Wobble = HandCutShape()
val Wobble2 = HandCutShape(true)

@Composable
fun reducedMotion(): Boolean {
    val resolver = LocalContext.current.contentResolver
    fun read() = Settings.Global.getFloat(resolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
    var reduced by remember(resolver) { mutableStateOf(read()) }
    DisposableEffect(resolver) {
        val observer = object : android.database.ContentObserver(android.os.Handler(android.os.Looper.getMainLooper())) {
            override fun onChange(selfChange: Boolean) { reduced = read() }
        }
        resolver.registerContentObserver(Settings.Global.getUriFor(Settings.Global.ANIMATOR_DURATION_SCALE), false, observer)
        onDispose { resolver.unregisterContentObserver(observer) }
    }
    return reduced
}

@Composable
fun markerText(text: String): String = if (LocalPowerkType.current.arabic) text else text.uppercase(Locale.ROOT)

@Composable
fun HighlightTitle(text: String, modifier: Modifier = Modifier) {
    val c = LocalPowerkColors.current
    Text(markerText(text), modifier.drawBehind {
        drawRect(c.highlighter, Offset(0f, if (c.fullHighlight) 0f else size.height * .52f),
            Size(size.width, size.height * if (c.fullHighlight) 1f else .38f))
    }.padding(horizontal = 6.dp), style = MaterialTheme.typography.headlineMedium,
        color = if (c.fullHighlight) c.onHighlighter else c.ink)
}

@Composable
fun PaperCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Card(modifier, shape = Wobble, border = BorderStroke(1.5.dp, LocalPowerkColors.current.ink),
        colors = CardDefaults.cardColors(containerColor = LocalPowerkColors.current.card), content = content)
}

// Keep native Material button interaction/accessibility; only change its shape, ink border and palette.
@Composable
fun Button(onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true,
    shape: Shape = Wobble2, colors: ButtonColors = ButtonDefaults.buttonColors(),
    border: BorderStroke? = BorderStroke(2.dp, LocalPowerkColors.current.ink), content: @Composable RowScope.() -> Unit) {
    androidx.compose.material3.Button(onClick, modifier, enabled, shape = shape, colors = colors, border = border, content = content)
}
@Composable
fun OutlinedButton(onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true,
    shape: Shape = Wobble2, colors: ButtonColors = ButtonDefaults.outlinedButtonColors(contentColor = LocalPowerkColors.current.ink),
    border: BorderStroke? = BorderStroke(2.dp, LocalPowerkColors.current.ink), content: @Composable RowScope.() -> Unit) {
    androidx.compose.material3.OutlinedButton(onClick, modifier, enabled, shape = shape, colors = colors, border = border, content = content)
}
@Composable
fun FilledTonalButton(onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true,
    content: @Composable RowScope.() -> Unit) {
    androidx.compose.material3.FilledTonalButton(onClick, modifier, enabled, shape = Wobble2,
        border = BorderStroke(2.dp, LocalPowerkColors.current.ink), content = content)
}
@Composable
fun TextButton(onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true,
    content: @Composable RowScope.() -> Unit) {
    androidx.compose.material3.TextButton(onClick, modifier, enabled,
        colors = ButtonDefaults.textButtonColors(contentColor = LocalPowerkColors.current.accentText), content = content)
}

@Composable
fun DashedDivider(modifier: Modifier = Modifier) {
    val color = LocalPowerkColors.current.line
    Canvas(modifier.fillMaxWidth().height(1.5.dp)) {
        drawLine(color, Offset(0f, size.height / 2), Offset(size.width, size.height / 2), 1.5.dp.toPx(),
            pathEffect = PathEffect.dashPathEffect(floatArrayOf(5.dp.toPx(), 4.dp.toPx())))
    }
}

@Composable
fun paperSegmentColors() = SegmentedButtonDefaults.colors(activeContainerColor = LocalPowerkColors.current.highlighter,
    activeContentColor = LocalPowerkColors.current.onHighlighter, inactiveContainerColor = Color.Transparent,
    inactiveContentColor = LocalPowerkColors.current.ink, activeBorderColor = LocalPowerkColors.current.ink,
    inactiveBorderColor = LocalPowerkColors.current.ink)

@Composable
fun paperSwitchColors() = SwitchDefaults.colors(checkedTrackColor = LocalPowerkColors.current.switchOn,
    checkedThumbColor = LocalPowerkColors.current.switchThumb, uncheckedTrackColor = LocalPowerkColors.current.fill,
    uncheckedThumbColor = LocalPowerkColors.current.switchThumb, uncheckedBorderColor = LocalPowerkColors.current.line)

fun wifiLevel(rssi: Int): Int = when { rssi >= -60 -> 0; rssi >= -70 -> 1; rssi >= -80 -> 2; else -> 3 }
val wifiLabels = listOf(R.string.wifi_excellent, R.string.wifi_good, R.string.wifi_weak, R.string.wifi_very_weak)

@Composable
fun StatusChip(online: Boolean) {
    val c = LocalPowerkColors.current
    val color = if (online) c.ok else c.bad
    Surface(shape = androidx.compose.foundation.shape.CircleShape, color = c.card,
        contentColor = color, border = BorderStroke(1.5.dp, color)) {
        Text(stringResource(if (online) R.string.state_online else R.string.state_offline),
            Modifier.padding(horizontal = 10.dp, vertical = 4.dp), style = MaterialTheme.typography.labelMedium)
    }
}

/** Western decimal figures, independently of the UI locale. */
fun decimal(value: Double, places: Int): String = String.format(Locale.US, "%.${places}f", value)

@Composable
fun StripInfoCard(strip: Strip) {
    PaperCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(markerText(stringResource(R.string.strip_info)), style = MaterialTheme.typography.titleLarge)
            Text(markerText(strip.displayName), style = MaterialTheme.typography.titleMedium)
            InfoRow(R.string.strip_connection, stringResource(if (strip.online) R.string.state_online else R.string.state_offline), false)
            strip.rssi?.let { InfoRow(R.string.wifi_signal, "${stringResource(wifiLabels[wifiLevel(it)])} ${isolateFigures("[$it dBm]")}", false)
                Text(stringResource(R.string.wifi_help), style = MaterialTheme.typography.bodySmall, color = LocalPowerkColors.current.ink2) }
            if (strip.ip.isNotBlank()) InfoRow(R.string.strip_ip, strip.ip)
            if (strip.model.isNotBlank()) InfoRow(R.string.strip_model, strip.model)
            if (strip.fw.isNotBlank()) InfoRow(R.string.strip_firmware, strip.fw)
            if (strip.mac.isNotBlank()) InfoRow(R.string.strip_mac, strip.mac)
        }
    }
}
@Composable
private fun InfoRow(label: Int, value: String, figures: Boolean = true) {
    DashedDivider()
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(label), Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
        Text(value, Modifier.weight(1.25f), style = if (figures) LocalPowerkType.current.figures.copy(fontSize = 13.sp)
            else MaterialTheme.typography.bodyMedium)
    }
}

@Composable
fun ThemePicker() {
    val context = LocalContext.current
    val choice = LocalThemeChoice.current
    PaperCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(markerText(stringResource(R.string.theme_title)), style = MaterialTheme.typography.titleLarge)
            val labels = listOf(R.string.theme_kraft, R.string.theme_clean, R.string.theme_night)
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                AppTheme.entries.forEachIndexed { index, theme ->
                    SegmentedButton(selected = choice.theme == theme, onClick = { ThemePrefs.save(context, choice.copy(theme = theme)) },
                        shape = Wobble2, colors = paperSegmentColors(), icon = {}) { Text(stringResource(labels[index])) }
                }
            }
            if (choice.theme == AppTheme.Clean) {
                val modes = listOf(R.string.lang_system, R.string.theme_light, R.string.theme_dark)
                SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                    CleanMode.entries.forEachIndexed { index, mode ->
                        SegmentedButton(selected = choice.mode == mode, onClick = { ThemePrefs.save(context, choice.copy(mode = mode)) },
                            shape = Wobble2, colors = paperSegmentColors(), icon = {}) { Text(stringResource(modes[index])) }
                    }
                }
            }
        }
    }
}
