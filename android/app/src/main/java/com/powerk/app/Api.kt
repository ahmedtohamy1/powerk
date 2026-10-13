package com.powerk.app

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

data class Outlet(val n: Int, val on: Boolean, val powerW: Double, val energyKwh: Double,
    val tempC: Int, val name: String = "", val alarms: List<String> = emptyList())
data class Strip(val mac: String, val name: String, val model: String, val fw: String,
    val online: Boolean, val on: Boolean, val powerW: Double, val energyKwh: Double,
    val voltage: Double?, val currentA: Double?, val rssi: Int?, val outlets: List<Outlet>,
    val label: String = "", val alarms: List<String> = emptyList(), val reportedAt: Long? = null,
    val todayKwh: Double = 0.0, val monthKwh: Double = 0.0, val monthCost: Double = 0.0,
    val ip: String = "") {
    val displayName: String get() = label.ifBlank { name }
}
data class Snapshot(val serverIp: String, val strips: List<Strip>, val currency: String = "",
    val priceKwh: Double = 0.0, val budget: Double? = null, val monthKwh: Double = 0.0,
    val monthCost: Double = 0.0, val unreadAlerts: Int = 0, val serverTime: Long? = null,
    val utcOffsetSeconds: Int = 0, val apiVersion: Int = 1, val port: Int = 0)
data class Rule(val id: String, val kind: String, val mac: String, val outlets: List<Int>,
    val enabled: Boolean = true, val label: String = "", val nextAt: Long? = null,
    val phase: String? = null, val at: String = "", val days: List<Int> = emptyList(),
    val on: Boolean = false, val fireAt: Long? = null, val onMin: Int = 1,
    val offMin: Int = 1, val belowW: Double = 1.0, val forMin: Int = 1)
data class Bucket(val t: Long, val kwh: Double, val cost: Double)
data class OutletEnergy(val n: Int, val kwh: Double)
data class History(val range: String, val unit: String, val currency: String,
    val buckets: List<Bucket>, val totalKwh: Double, val totalCost: Double,
    val perOutlet: List<OutletEnergy>)
data class ServerSettings(val currency: String = "", val priceKwh: Double = 0.0,
    val budget: Double? = null, val tempMaxC: Double = 60.0, val powerMaxW: Double = 3500.0,
    val ntfyUrl: String = "", val telegramChat: String = "", val telegramConfigured: Boolean = false)
data class Event(val id: Long, val ts: Long, val level: String, val code: String,
    val mac: String, val outlet: Int, val text: String, val params: JSONObject)
data class EventPage(val events: List<Event>, val unreadAlerts: Int)
data class FailedChannel(val channel: String, val error: String)
data class NotificationResult(val ok: Boolean, val sent: List<String>,
    val failed: List<FailedChannel>, val error: String)

/** Targets are always identified by number, never by position or name. */
fun Strip.outletOn(n: Int): Boolean = outlet(n)?.on ?: false
fun Strip.outlet(n: Int): Outlet? = outlets.firstOrNull { it.n == n }
class ServerException(message: String, val status: Int = 0, val messageRes: Int? = null) : Exception(message)

object Api {
    private suspend fun call(base: String, token: String, path: String,
        method: String = "GET", body: JSONObject? = null): JSONObject = withContext(Dispatchers.IO) {
        JSONObject(request("$base/api/$path", body?.toString(), token, method))
    }
    suspend fun snapshot(base: String, token: String) = parseSnapshot(call(base, token, "state"))
    suspend fun setOutlet(base: String, token: String, mac: String, outlet: Int, on: Boolean): Boolean {
        val response = call(base, token, "onoff", "POST", JSONObject()
            .put("mac", mac).put("outlet", outlet).put("on", on))
        if (!response.optBoolean("ok")) throw ServerException(response.optString("error", "command failed"))
        return commandConfirmed(response)
    }
    internal fun commandConfirmed(response: JSONObject): Boolean = response.optBoolean("confirmed", true)
    suspend fun rename(base: String, token: String, mac: String, outlet: Int, name: String) {
        call(base, token, "name", "POST", JSONObject().put("mac", mac).put("outlet", outlet).put("name", name))
    }
    suspend fun rules(base: String, token: String, mac: String? = null): List<Rule> =
        parseRules(call(base, token, "rules" + (mac?.let { "?mac=${encode(it)}" } ?: "")))
    suspend fun createRule(base: String, token: String, body: JSONObject): Rule =
        parseRule(call(base, token, "rules", "POST", body).getJSONObject("rule"))
    suspend fun updateRule(base: String, token: String, id: String, body: JSONObject): Rule =
        parseRule(call(base, token, "rules/${encode(id)}", "PUT", body).getJSONObject("rule"))
    suspend fun deleteRule(base: String, token: String, id: String) {
        call(base, token, "rules/${encode(id)}", "DELETE")
    }
    suspend fun history(base: String, token: String, mac: String, range: String, outlet: Int? = null): History =
        parseHistory(call(base, token, "history?mac=${encode(mac)}&range=${encode(range)}" +
            (outlet?.let { "&outlet=$it" } ?: "")))
    suspend fun settings(base: String, token: String): ServerSettings = parseSettings(call(base, token, "settings"))
    suspend fun saveSettings(base: String, token: String, body: JSONObject): ServerSettings =
        parseSettings(call(base, token, "settings", "PUT", body))
    suspend fun events(base: String, token: String, beforeId: Long? = null, limit: Int = 50): EventPage =
        parseEvents(call(base, token, "events?limit=$limit" + (beforeId?.let { "&before_id=$it" } ?: "")))
    suspend fun readEvents(base: String, token: String, throughId: Long? = null): Int =
        call(base, token, "events/read", "POST", JSONObject().apply {
            throughId?.let { put("through_id", it) }
        }).optInt("unread_alerts")
    suspend fun testAlerts(base: String, token: String): NotificationResult =
        parseNotification(call(base, token, "alerts/test", "POST", JSONObject()))

    internal fun request(url: String, body: String?, token: String,
        method: String = if (body == null) "GET" else "POST"): String {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 4000
            connection.readTimeout = 15000
            connection.requestMethod = method
            if (token.isNotBlank()) connection.setRequestProperty("X-Token", token)
            if (method == "POST" || method == "PUT") {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write((body ?: "{}").toByteArray(Charsets.UTF_8)) }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) throw ServerException(errorOf(text, code), code)
            return text
        } catch (e: ServerException) { throw e
        } catch (e: Exception) { throw ServerException(e.message ?: "connection failed")
        } finally { connection.disconnect() }
    }
    private fun encode(value: String) = URLEncoder.encode(value, "UTF-8")
    private fun errorOf(body: String, code: Int): String = try {
        JSONObject(body).optString("error").ifBlank { "HTTP $code" }
    } catch (_: Exception) { "HTTP $code" }

    internal fun parseSnapshot(o: JSONObject) = Snapshot(o.optString("local_ip"),
        o.objects("devices").map(::parseStrip), o.text("currency"), o.number("price_kwh"),
        o.nullableNumber("budget"), o.number("month_kwh"), o.number("month_cost"),
        o.optInt("unread_alerts"), o.longOrNull("server_time"), o.optInt("utc_offset_seconds"),
        o.optInt("api_version", 1), o.optInt("port"))
    private fun parseStrip(o: JSONObject) = Strip(o.text("mac"), o.text("name"), o.text("model"),
        o.text("fw"), o.optBoolean("online"), o.optBoolean("on"), o.number("power_w"),
        o.number("energy_kwh"), o.nullableNumber("voltage"), o.nullableNumber("current_a"),
        if (o.isNull("rssi")) null else o.optInt("rssi"), o.objects("outlets").map { x ->
            Outlet(x.optInt("n"), x.optBoolean("on"), x.number("power_w"), x.number("energy_kwh"),
                x.optInt("temp_c"), x.text("name"), x.alarms())
        }, o.text("label"), o.alarms(), o.longOrNull("reported_at"), o.number("today_kwh"),
        o.number("month_kwh"), o.number("month_cost"), o.text("ip"))
    internal fun parseRules(o: JSONObject) = o.objects("rules").map(::parseRule)
    internal fun parseRule(o: JSONObject) = Rule(o.text("id"), o.text("kind"), o.text("mac"),
        o.ints("outlets"), o.optBoolean("enabled", true), o.text("label"), o.longOrNull("next_at"),
        o.text("phase").ifBlank { null }, o.text("at"), o.ints("days"), o.optBoolean("on"),
        o.longOrNull("fire_at"), o.optInt("on_min", 1), o.optInt("off_min", 1),
        o.number("below_w", 1.0), o.optInt("for_min", 1))
    internal fun parseHistory(o: JSONObject) = History(o.text("range"), o.text("unit"), o.text("currency"),
        o.objects("buckets").map { Bucket(it.optLong("t"), it.number("kwh"), it.number("cost")) },
        o.number("total_kwh"), o.number("total_cost"),
        o.objects("per_outlet").map { OutletEnergy(it.optInt("n"), it.number("kwh")) })
    internal fun parseSettings(o: JSONObject) = ServerSettings(o.text("currency"), o.number("price_kwh"),
        o.nullableNumber("budget"), o.number("temp_max_c", 60.0), o.number("power_max_w", 3500.0),
        o.text("ntfy_url"), o.text("telegram_chat"), o.optBoolean("telegram_configured"))
    internal fun parseEvents(o: JSONObject) = EventPage(o.objects("events").map {
        Event(it.optLong("id"), it.optLong("ts"), it.text("level"), it.text("code"), it.text("mac"),
            it.optInt("outlet"), it.text("text"), it.optJSONObject("params") ?: JSONObject())
    }.sortedByDescending { it.id }, o.optInt("unread_alerts"))
    internal fun parseNotification(o: JSONObject) = NotificationResult(o.optBoolean("ok"), o.strings("sent"),
        o.objects("failed").map { FailedChannel(it.text("channel"), it.text("error")) }, o.text("error"))
}

internal fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key)
internal fun JSONObject.number(key: String, default: Double = 0.0): Double =
    optDouble(key, default).takeIf { it.isFinite() } ?: default
internal fun JSONObject.nullableNumber(key: String): Double? =
    if (isNull(key)) null else optDouble(key).takeIf { it.isFinite() }
internal fun JSONObject.longOrNull(key: String): Long? = if (isNull(key)) null else optLong(key)
internal fun JSONObject.objects(key: String): List<JSONObject> = optJSONArray(key)?.let { a ->
    (0 until a.length()).mapNotNull { a.optJSONObject(it) }
}.orEmpty()
internal fun JSONObject.strings(key: String): List<String> = optJSONArray(key)?.let { a ->
    (0 until a.length()).mapNotNull { if (a.isNull(it)) null else a.optString(it).ifBlank { null } }
}.orEmpty()
internal fun JSONObject.ints(key: String): List<Int> = optJSONArray(key)?.let { a ->
    (0 until a.length()).map { a.optInt(it) }
}.orEmpty()
private fun JSONObject.alarms(): List<String> = strings("alarms").ifEmpty {
    text("alarm").ifBlank { null }?.let(::listOf).orEmpty()
}
