package com.powerk.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import android.util.SizeF
import android.view.View
import android.widget.RemoteViews
import androidx.compose.ui.graphics.toArgb
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.util.concurrent.ConcurrentHashMap
import java.util.Locale
import kotlin.math.roundToInt

class StripWidget : PowerkWidget()
class UsageWidget : PowerkWidget()

object StripWidgetPrefs {
    private fun prefs(context: Context) = context.applicationContext.getSharedPreferences("targets", Context.MODE_PRIVATE)
    fun read(context: Context, id: Int): String? = prefs(context).getString("strip_widget_$id", null)
    fun save(context: Context, id: Int, mac: String?) {
        prefs(context).edit().apply {
            if (mac == null) remove("strip_widget_$id") else putString("strip_widget_$id", mac)
        }.apply()
    }
}

/** A refresh only reads. Commands only enter through the explicit cell/root tap action. */
open class PowerkWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        val result = goAsync()
        WidgetUpdates.scope.launch {
            try { WidgetUpdates.refresh(context) } finally { result.finish() }
        }
    }
    override fun onAppWidgetOptionsChanged(context: Context, manager: AppWidgetManager, id: Int, options: Bundle) {
        WidgetUpdates.render(context, javaClass, id)
    }
    override fun onDeleted(context: Context, ids: IntArray) {
        ids.forEach { TargetPrefs.setWidget(context, it, null); StripWidgetPrefs.save(context, it, null) }
    }
    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action != WidgetUpdates.ACTION_TOGGLE) return
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        val info = AppWidgetManager.getInstance(context).getAppWidgetInfo(id) ?: return
        if (info.provider != ComponentName(context, javaClass)) return
        val target = when (this) {
            is OutletWidget -> TargetPrefs.widget(context, id)
            is StripWidget -> StripWidgetPrefs.read(context, id)?.let { mac ->
                val n = intent.getIntExtra("outlet", 0)
                if (n in 1..4) OutletTarget(mac, n, "") else null
            }
            else -> null
        } ?: return
        val key = "${target.mac}:${target.n}"
        if (!WidgetUpdates.pending.add(key)) return
        WidgetUpdates.renderAll(context)
        val result = goAsync()
        WidgetUpdates.scope.launch {
            var message: String? = null
            val connection = Server.connection
            try {
                val outcome = OutletControl.toggle(target)
                if (outcome != null && connection.isCurrent(Server.connection)) {
                    WidgetUpdates.freshSnapshot(context, outcome.snapshot, connection)
                    if (!outcome.confirmed) message = context.localized().getString(R.string.not_confirmed)
                }
            } catch (_: Exception) {
                // Never retry an unknown outcome; the next scheduled update is read-only.
                WidgetUpdates.clearSnapshot()
                message = context.localized().getString(R.string.not_confirmed)
            } finally {
                WidgetUpdates.pending.remove(key)
                WidgetUpdates.renderAll(context)
                if (message != null) WidgetUpdates.render(context, this@PowerkWidget.javaClass, id, message)
                result.finish()
            }
        }
    }
}

object WidgetUpdates {
    const val ACTION_TOGGLE = "com.powerk.app.WIDGET_TOGGLE"
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    val pending: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private data class Cached(val snapshot: Snapshot, val connection: ServerConnection)
    @Volatile private var cached: Cached? = null
    private val providers = listOf(OutletWidget::class.java, StripWidget::class.java, UsageWidget::class.java)
    fun clearSnapshot() { cached = null }
    fun freshSnapshot(context: Context, snapshot: Snapshot, connection: ServerConnection) {
        if (!connection.isCurrent(Server.connection)) return
        cached = Cached(snapshot, connection)
        renderAll(context)
    }
    fun appearanceChanged(context: Context) { renderAll(context) }
    suspend fun refresh(context: Context) {
        val connection = Server.connection
        try { freshSnapshot(context, OutletControl.fresh(), connection) }
        catch (_: Exception) {
            if (connection.isCurrent(Server.connection)) { cached = null; renderAll(context) }
        }
    }
    fun renderAll(context: Context) {
        val manager = AppWidgetManager.getInstance(context)
        providers.forEach { provider -> manager.getAppWidgetIds(ComponentName(context, provider)).forEach { render(context, provider, it) } }
    }
    fun render(context: Context, provider: Class<out AppWidgetProvider>, id: Int, message: String? = null) {
        val manager = AppWidgetManager.getInstance(context)
        val snapshot = cached?.takeIf { it.connection.isCurrent(Server.connection) }?.snapshot
        val c = widgetColors(context)
        val options = manager.getAppWidgetOptions(id)
        fun views(wide: Boolean, compact: Boolean = false, minimum: Boolean = false): RemoteViews = when (provider) {
            OutletWidget::class.java -> outletViews(context, id, snapshot, c, message,
                options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 150) < 110)
            StripWidget::class.java -> stripViews(context, id, snapshot, c, wide, message, compact)
            else -> usageViews(context, id, snapshot, c, wide, compact, minimum)
        }
        val remote = if (Build.VERSION.SDK_INT >= 31 && provider != OutletWidget::class.java) {
            RemoteViews(mapOf(SizeF(110f, 110f) to views(false, true, true), SizeF(150f, 150f) to views(false, true), SizeF(200f, 200f) to views(false),
                SizeF(220f, 70f) to views(true, true), SizeF(380f, 110f) to views(true)))
        } else {
            val width = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 200)
            val height = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 200)
            views(widgetWide(width, height), height < 110 || width < 180, width < 140 && height < 140)
        }
        manager.updateAppWidget(id, remote)
    }
}

fun widgetColors(context: Context): PowerkColors = powerkColors(ThemePrefs.read(context),
    context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK == Configuration.UI_MODE_NIGHT_YES)

fun widgetWide(width: Int, height: Int): Boolean = width >= 280 || width >= 220 && height < 140
private fun Context.widgetPx(dp: Int): Int = (dp * resources.displayMetrics.density).roundToInt()

private fun background(context: Context, on: Boolean = false): Int {
    val choice = ThemePrefs.read(context)
    return when (choice.theme) {
        AppTheme.Kraft -> if (on) R.drawable.widget_kraft_on else R.drawable.widget_kraft
        AppTheme.Night -> if (on) R.drawable.widget_night_on else R.drawable.widget_night
        AppTheme.Clean -> if (widgetColors(context).dark) {
            if (on) R.drawable.widget_clean_dark_on else R.drawable.widget_clean_dark
        } else if (on) R.drawable.widget_clean_on else R.drawable.widget_clean
    }
}
private fun RemoteViews.paint(context: Context, root: Int, names: IntArray, secondary: IntArray,
    c: PowerkColors, on: Boolean = false) {
    setInt(root, "setBackgroundResource", background(context, on))
    setInt(root, "setLayoutDirection", if (context.localized().resources.configuration.locales[0].language == "ar")
        View.LAYOUT_DIRECTION_RTL else View.LAYOUT_DIRECTION_LTR)
    names.forEach {
        setTextColor(it, (if (on) c.onAccent else c.ink).toArgb())
    }
    secondary.forEach {
        setTextColor(it, (if (on) c.onAccentSecondary else c.ink2).toArgb())
    }
}
private fun widgetMarker(context: Context, text: String) =
    if (context.resources.configuration.locales[0].language == "ar") text else text.uppercase(Locale.ROOT)
private fun Context.widgetString(res: Int, vararg args: Any): String = String.format(Locale.US, getText(res).toString(), *args)
/** Isolate an entire measurement so units and signs don't reverse inside an Arabic sentence. */
fun isolateFigures(value: String): String = "\u2066$value\u2069"
private fun Context.onWatts(watts: Double): String {
    val value = decimal(watts, 1)
    return getString(R.string.widget_on_watts, value).replace("$value W", isolateFigures("$value W"))
}
private fun Context.costString(res: Int, value: String, currency: String): String =
    getString(res, value, currency).replace("$value $currency", isolateFigures("$value $currency"))
private fun openApp(context: Context, id: Int, energy: Boolean = false): PendingIntent = PendingIntent.getActivity(context,
    id, Intent(context, MainActivity::class.java).setAction(if (energy) "powerk.energy" else "powerk.home")
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        .putExtra(MainActivity.EXTRA_TAB, if (energy) "energy" else "plugs"),
    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
private fun toggleIntent(context: Context, provider: Class<out AppWidgetProvider>, id: Int, n: Int): PendingIntent =
    PendingIntent.getBroadcast(context, id, Intent(context, provider).setAction(WidgetUpdates.ACTION_TOGGLE)
        .setData(android.net.Uri.parse("powerk://widget/$id/$n"))
        .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id).putExtra("outlet", n),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

fun outletViews(context: Context, id: Int, snapshot: Snapshot?, c: PowerkColors = widgetColors(context), message: String? = null,
    compact: Boolean = false): RemoteViews {
    val local = context.localized()
    val target = TargetPrefs.widget(context, id)
    val resolved = target?.let { snapshot?.resolve(it) }
    val online = resolved?.first?.online == true
    val on = online && resolved?.second?.on == true
    val switching = target != null && "${target.mac}:${target.n}" in WidgetUpdates.pending
    return RemoteViews(context.packageName, if (compact) R.layout.outlet_widget_compact else R.layout.outlet_widget).apply {
        paint(context, R.id.widget_root, intArrayOf(R.id.widget_name), intArrayOf(R.id.widget_state), c, on)
        setInt(R.id.widget_power, "setColorFilter", (if (on) c.onAccent else c.ink).toArgb())
        setInt(R.id.widget_power, "setImageAlpha", if (online) 255 else 140)
        if (!online) {
            setTextColor(R.id.widget_name, c.ink.copy(alpha = .6f).toArgb())
            setTextColor(R.id.widget_state, c.ink2.copy(alpha = .6f).toArgb())
        }
        setTextViewText(R.id.widget_name, widgetMarker(local, resolved?.second?.displayName(local) ?: target?.name?.ifBlank {
            local.widgetString(R.string.outlet_n, target.n) } ?: local.getString(R.string.choose_outlet)))
        setTextViewText(R.id.widget_state, message ?: if (switching) local.getString(R.string.switching)
            else if (!online) local.getString(R.string.state_offline)
            else if (compact) isolateFigures("${decimal(resolved!!.second.powerW, 1)} W") else if (on)
                local.onWatts(resolved!!.second.powerW) else local.getString(R.string.state_off))
        val action = if (target == null) PendingIntent.getActivity(context, id, Intent(context, WidgetConfigActivity::class.java)
            .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            else toggleIntent(context, OutletWidget::class.java, id, target.n)
        setOnClickPendingIntent(R.id.widget_root, action)
        setContentDescription(R.id.widget_root, local.getString(R.string.outlet_widget_description))
    }
}

private val cellIds = intArrayOf(R.id.strip_cell_1, R.id.strip_cell_2, R.id.strip_cell_3, R.id.strip_cell_4)
private val nameIds = intArrayOf(R.id.strip_name_1, R.id.strip_name_2, R.id.strip_name_3, R.id.strip_name_4)
private val stateIds = intArrayOf(R.id.strip_state_1, R.id.strip_state_2, R.id.strip_state_3, R.id.strip_state_4)
fun stripViews(context: Context, id: Int, snapshot: Snapshot?, c: PowerkColors = widgetColors(context), wide: Boolean = true,
    message: String? = null, compact: Boolean = false): RemoteViews {
    val local = context.localized()
    var mac = StripWidgetPrefs.read(context, id)
    if (mac == null && snapshot?.strips?.size == 1) {
        mac = snapshot.strips.single().mac; StripWidgetPrefs.save(context, id, mac)
    }
    val strip = snapshot?.strips?.find { it.mac == mac }
    val layout = if (wide && compact) R.layout.strip_widget_short else if (wide) R.layout.strip_widget else R.layout.strip_widget_narrow
    return RemoteViews(context.packageName, layout).apply {
        paint(context, R.id.widget_root, intArrayOf(R.id.widget_name), intArrayOf(R.id.widget_state), c)
        if (compact) {
            val pad = context.widgetPx(8)
            setViewPadding(R.id.widget_root, pad, pad, pad, pad)
            setTextViewTextSize(R.id.widget_name, android.util.TypedValue.COMPLEX_UNIT_SP, 7f)
            setTextViewTextSize(R.id.widget_state, android.util.TypedValue.COMPLEX_UNIT_SP, 9f)
        }
        setTextViewText(R.id.widget_name, widgetMarker(local, strip?.displayName ?: local.getString(R.string.strip_widget_title)))
        setTextViewText(R.id.widget_state, if (strip?.online == true) "${decimal(strip.powerW, 1)} W" else local.getString(R.string.state_offline))
        setOnClickPendingIntent(R.id.strip_header, if (mac == null) PendingIntent.getActivity(context, id,
            Intent(context, StripWidgetConfigActivity::class.java).putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE) else openApp(context, id))
        (1..4).forEach { n ->
            val outlet = strip?.outlet(n)
            val online = strip?.online == true && outlet != null
            val on = online && outlet?.on == true
            paint(context, cellIds[n - 1], intArrayOf(nameIds[n - 1]), intArrayOf(stateIds[n - 1]), c, on)
            setInt(nameIds[n - 1], "setMaxLines", if (compact) 1 else 2)
            if (compact) {
                val pad = 0
                setViewPadding(cellIds[n - 1], context.widgetPx(8), pad, context.widgetPx(8), pad)
                setTextViewTextSize(nameIds[n - 1], android.util.TypedValue.COMPLEX_UNIT_SP, 7f)
                setTextViewTextSize(stateIds[n - 1], android.util.TypedValue.COMPLEX_UNIT_SP, 10f)
            }
            setTextViewText(nameIds[n - 1], widgetMarker(local, outlet?.displayName(local) ?: local.widgetString(R.string.outlet_n, n)))
            setTextViewText(stateIds[n - 1], if ("$mac:$n" in WidgetUpdates.pending) local.getString(R.string.switching)
                else message ?: if (!online) local.getString(R.string.state_offline) else if (on)
                      local.onWatts(outlet!!.powerW).replace(" W", "\u00A0W")
                    else local.getString(R.string.state_off))
            setContentDescription(cellIds[n - 1], "${outlet?.displayName(local) ?: local.widgetString(R.string.outlet_n, n)} · ${local.getString(R.string.widget_toggle)}")
            setOnClickPendingIntent(cellIds[n - 1], if (mac != null) toggleIntent(context, StripWidget::class.java, id, n) else openApp(context, id))
        }
    }
}

fun usageViews(context: Context, id: Int, snapshot: Snapshot?, c: PowerkColors = widgetColors(context), wide: Boolean = false,
    compact: Boolean = false, minimum: Boolean = false): RemoteViews {
    val local = context.localized()
    val today = snapshot?.strips?.sumOf { it.todayKwh } ?: 0.0
    val online = snapshot != null && snapshot.strips.isNotEmpty() && snapshot.strips.all { it.online }
    return RemoteViews(context.packageName, if (wide) R.layout.usage_widget_wide else R.layout.usage_widget).apply {
        paint(context, R.id.widget_root, intArrayOf(R.id.widget_name, R.id.usage_watts),
            intArrayOf(R.id.usage_today, R.id.usage_today_cost, R.id.usage_month_cost, R.id.usage_budget), c)
        if (compact) {
            val pad = context.widgetPx(6)
            setViewPadding(R.id.widget_root, pad, pad, pad, pad)
            setTextViewTextSize(R.id.widget_name, android.util.TypedValue.COMPLEX_UNIT_SP, if (minimum) 8f else if (wide) 12f else 10f)
            setTextViewTextSize(R.id.usage_watts, android.util.TypedValue.COMPLEX_UNIT_SP, if (minimum) 14f else if (wide) 18f else 16f)
            intArrayOf(R.id.usage_today, R.id.usage_today_cost, R.id.usage_month_cost, R.id.usage_budget).forEach {
                setTextViewTextSize(it, android.util.TypedValue.COMPLEX_UNIT_SP, if (minimum || wide) 8f else 9f)
                setInt(it, "setMaxLines", 1)
            }
        }
        setTextViewText(R.id.widget_name, widgetMarker(local, local.getString(R.string.usage_widget_title)))
        setTextViewText(R.id.usage_watts, if (online) "${decimal(snapshot!!.strips.sumOf { it.powerW }, 1)} W" else local.getString(R.string.state_offline))
        setTextViewText(R.id.usage_today, "${local.getString(R.string.summary_today)} · ${isolateFigures("${if (snapshot == null) "—" else decimal(today, 2)} kWh")}")
        setTextViewText(R.id.usage_today_cost, local.costString(R.string.widget_today_cost,
            if (snapshot == null) "—" else decimal(today * snapshot.priceKwh, 1), snapshot?.currency.orEmpty()))
        setTextViewText(R.id.usage_month_cost, local.costString(R.string.widget_month_cost,
            if (snapshot == null) "—" else decimal(snapshot.monthCost, 1), snapshot?.currency.orEmpty()))
        val pct = snapshot?.let { budgetPercentage(it.monthCost, it.budget) }
        setViewVisibility(R.id.usage_budget, if (pct == null) View.GONE else View.VISIBLE)
        setTextViewText(R.id.usage_budget, pct?.let {
            val value = "${it.toInt()}%"
            local.widgetString(R.string.widget_budget, it.toInt()).replace(value, isolateFigures(value))
        }.orEmpty())
        setTextColor(R.id.usage_budget, (if ((pct ?: 0.0) >= 90) c.bad else if ((pct ?: 0.0) >= 75) c.warn else c.ok).toArgb())
        setOnClickPendingIntent(R.id.widget_root, openApp(context, id, true))
        setContentDescription(R.id.widget_root, local.getString(R.string.usage_widget_description))
    }
}
