package com.powerk.app

import android.content.Context
import android.content.res.Resources
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.Locale

fun budgetPercentage(cost: Double, budget: Double?): Double? =
    budget?.takeIf { it.isFinite() && it > 0 && cost.isFinite() }?.let { (cost / it * 100).coerceAtLeast(0.0) }

/** Use the offset supplied by the server, never the phone's time zone. */
fun serverLocalTime(epoch: Long?, offset: Int, pattern: String = "dd MMM HH:mm", locale: Locale = Locale.getDefault()): String {
    if (epoch == null) return ""
    val zone = ZoneOffset.ofTotalSeconds(offset.coerceIn(-64800, 64800))
    return DateTimeFormatter.ofPattern(pattern, locale).withZone(zone).format(Instant.ofEpochSecond(epoch))
}

val weekdayStrings = listOf(R.string.mon, R.string.tue, R.string.wed, R.string.thu, R.string.fri, R.string.sat, R.string.sun)
val ruleKinds = listOf("schedule", "timer", "cycle", "autooff")
fun kindString(kind: String) = when (kind) {
    "schedule" -> R.string.kind_schedule
    "timer" -> R.string.kind_timer
    "cycle" -> R.string.kind_cycle
    "autooff" -> R.string.kind_autooff
    else -> R.string.tab_rules
}
fun alarmString(code: String) = when (code) {
    "temp_high" -> R.string.alarm_temp
    "power_high" -> R.string.alarm_power
    else -> R.string.alarm_generic
}
fun Outlet.displayName(context: Context) = displayName(context.resources)
fun Outlet.displayName(resources: Resources) = name.ifBlank { resources.getString(R.string.outlet_n, n) }
fun uiError(context: Context, error: Exception) = uiError(context.resources, error)
fun uiError(resources: Resources, error: Exception): String {
    if (error is kotlinx.coroutines.CancellationException) throw error
    if (error is ServerException && error.messageRes != null) return resources.getString(error.messageRes)
    return if (error is ServerException && error.status == 404) resources.getString(R.string.update_server)
    else error.message ?: resources.getString(R.string.command_failed)
}

/** Translation injected so formatting remains pure and JVM-testable. */
fun ruleDescription(rule: Rule, offset: Int, outletNames: String, text: (Int, List<Any>) -> String): String {
    val on = text(if (rule.on) R.string.state_on else R.string.state_off, emptyList())
    val body = when (rule.kind) {
        "schedule" -> text(R.string.desc_schedule, listOf(rule.at, on,
            rule.days.filter { it in 0..6 }.sorted().joinToString(" ") { text(weekdayStrings[it], emptyList()) }))
        "timer" -> text(R.string.desc_timer, listOf(on, serverLocalTime(rule.fireAt ?: rule.nextAt, offset, "HH:mm")))
        "cycle" -> text(R.string.desc_cycle, listOf(rule.onMin, rule.offMin,
            text(when (rule.phase) { "on" -> R.string.state_on; "off" -> R.string.state_off; else -> R.string.state_pending }, emptyList())))
        "autooff" -> text(R.string.desc_autooff, listOf(rule.belowW.toString(), rule.forMin))
        else -> rule.kind
    }
    return listOf(body, outletNames).filter { it.isNotBlank() }.joinToString(" · ")
}
fun Resources.describe(rule: Rule, strip: Strip?, offset: Int): String = ruleDescription(rule, offset,
    (if (0 in rule.outlets) (1..4).toList() else rule.outlets).joinToString(", ") { n ->
        strip?.outlet(n)?.displayName(this) ?: getString(R.string.outlet_n, n)
    }) { id, args -> getString(id, *args.toTypedArray()) }

fun Resources.eventText(event: Event): String {
    val p = event.params
    val on = getString(if (p.optBoolean("on")) R.string.state_on else R.string.state_off)
    return when (event.code) {
        "offline" -> getString(R.string.event_offline)
        "online" -> getString(R.string.event_online)
        "temp_high" -> getString(R.string.event_temp, p.number("temp_c").toString(), p.number("watts").toString())
        "power_high" -> getString(R.string.event_power, p.number("watts").toString())
        "budget" -> getString(R.string.event_budget, p.number("pct").toString())
        "rule_fired" -> getString(R.string.event_fired, on)
        "rule_skipped" -> getString(R.string.event_skipped, on)
        "rule_failed" -> getString(R.string.event_failed, on)
        "autooff" -> getString(R.string.event_autooff, p.number("watts").toString())
        else -> event.text
    }
}

data class RuleDraft(val kind: String, val mac: String, val outlets: Set<Int>, val label: String,
    val at: String, val days: Set<Int>, val on: Boolean, val minutes: String,
    val onMin: String, val offMin: String, val belowW: String, val forMin: String) {
    fun valid(): Boolean {
        if (mac.isBlank() || outlets.isEmpty() || outlets.any { it !in 1..4 } || label.length > 40) return false
        fun inRange(s: String, max: Int) = s.apiInt()?.let { it in 1..max } == true
        return when (kind) {
            "schedule" -> Regex("([01][0-9]|2[0-3]):[0-5][0-9]").matches(normalizeNumber(at)) &&
                days.isNotEmpty() && days.all { it in 0..6 }
            "timer" -> inRange(minutes, 10080)
            "cycle" -> inRange(onMin, 1440) && inRange(offMin, 1440)
            "autooff" -> belowW.apiDouble()?.let { it > 0 && it <= 3500 } == true && inRange(forMin, 1440)
            else -> false
        }
    }
    fun body(): JSONObject {
        require(valid())
        return JSONObject().put("kind", kind).put("mac", mac).put("outlets", JSONArray(outlets.sorted()))
            .put("label", label).apply {
                when (kind) {
                    "schedule" -> put("at", normalizeNumber(at)).put("days", JSONArray(days.sorted())).put("on", on)
                    "timer" -> put("minutes", minutes.apiInt()).put("on", on)
                    "cycle" -> put("on_min", onMin.apiInt()).put("off_min", offMin.apiInt())
                    "autooff" -> put("below_w", belowW.apiDouble()).put("for_min", forMin.apiInt())
                }
            }
    }
}

/** Arabic and Persian digits, decimal separator, and bidi marks from localized keyboards. */
fun normalizeNumber(value: String): String = buildString {
    value.trim().forEach { c ->
        val digit = Character.digit(c, 10)
        when {
            digit >= 0 -> append('0' + digit)
            c == '\u066B' -> append('.')
            c == '\u066C' || c == '\u200E' || c == '\u200F' || c == '\u061C' -> Unit
            else -> append(c)
        }
    }
}
fun String.apiInt(): Int? = normalizeNumber(this).toIntOrNull()
fun String.apiDouble(): Double? = normalizeNumber(this).toDoubleOrNull()?.takeIf { it.isFinite() }

fun newRuleMac(selected: String, available: List<String>): String =
    selected.takeIf { it in available } ?: available.firstOrNull().orEmpty()

/** Empty replacement keeps the write-only secret; clear is a separate, deliberate action. */
fun settingsBody(settings: ServerSettings, replacementToken: String, clearToken: Boolean): JSONObject =
    JSONObject().put("currency", settings.currency).put("price_kwh", settings.priceKwh)
        .put("budget", settings.budget ?: JSONObject.NULL).put("temp_max_c", settings.tempMaxC)
        .put("power_max_w", settings.powerMaxW).put("ntfy_url", settings.ntfyUrl)
        .put("telegram_chat", settings.telegramChat).apply {
            if (clearToken) put("telegram_token", "")
            else if (replacementToken.isNotEmpty()) put("telegram_token", replacementToken)
        }
