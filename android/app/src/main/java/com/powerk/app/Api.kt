package com.powerk.app

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

data class Outlet(
    val n: Int,
    val on: Boolean,
    val powerW: Double,
    val energyKwh: Double,
    val tempC: Int,
)

data class Strip(
    val mac: String,
    val name: String,
    val model: String,
    val fw: String,
    val online: Boolean,
    val on: Boolean,
    val powerW: Double,
    val energyKwh: Double,
    val voltage: Double?,
    val currentA: Double?,
    val rssi: Int?,
    val outlets: List<Outlet>,
)

data class Snapshot(val serverIp: String, val strips: List<Strip>)

/** Outlet by its number (1..4) - never by list position. */
fun Strip.outletOn(n: Int): Boolean = outlets.firstOrNull { it.n == n }?.on ?: false

fun Strip.outlet(n: Int): Outlet? = outlets.firstOrNull { it.n == n }

class ServerException(message: String) : Exception(message)

/** Talks to the powerk.py JSON API: GET /api/state, POST /api/onoff. */
object Api {

    suspend fun snapshot(base: String, token: String): Snapshot = withContext(Dispatchers.IO) {
        val root = JSONObject(request("$base/api/state", null, token))
        val array = root.optJSONArray("devices")
        val strips = (0 until (array?.length() ?: 0)).map { parseStrip(array!!.getJSONObject(it)) }
        Snapshot(root.optString("local_ip"), strips)
    }

    suspend fun setOutlet(base: String, token: String, mac: String, outlet: Int, on: Boolean) = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("mac", mac)
            .put("outlet", outlet)
            .put("on", on)
            .toString()
        val response = JSONObject(request("$base/api/onoff", body, token))
        if (!response.optBoolean("ok")) {
            throw ServerException(response.optString("error").ifBlank { "command failed" })
        }
    }

    internal fun request(url: String, body: String?, token: String): String {
        val connection = (URL(url).openConnection() as HttpURLConnection)
        try {
            connection.connectTimeout = 4000
            connection.readTimeout = 12000
            connection.instanceFollowRedirects = false
            connection.requestMethod = if (body == null) "GET" else "POST"
            if (token.isNotBlank()) connection.setRequestProperty("X-Token", token)
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) throw ServerException(errorOf(text, code))
            return text
        } catch (e: CancellationException) {
            throw e
        } catch (e: ServerException) {
            throw e
        } catch (e: Exception) {
            throw ServerException(e.message ?: "connection failed")
        } finally {
            connection.disconnect()
        }
    }

    private fun errorOf(body: String, code: Int): String =
        try {
            JSONObject(body).optString("error").ifBlank { "HTTP $code" }
        } catch (e: Exception) {
            "HTTP $code"
        }

    private fun parseStrip(o: JSONObject): Strip {
        val array = o.optJSONArray("outlets")
        val outlets = (0 until (array?.length() ?: 0)).map { i ->
            val x = array!!.getJSONObject(i)
            Outlet(
                n = x.optInt("n"),
                on = x.optBoolean("on"),
                powerW = x.optDouble("power_w", 0.0),
                energyKwh = x.optDouble("energy_kwh", 0.0),
                tempC = x.optInt("temp_c"),
            )
        }
        return Strip(
            mac = o.optString("mac"),
            name = o.optString("name"),
            model = o.optString("model"),
            fw = o.optString("fw"),
            online = o.optBoolean("online"),
            on = o.optBoolean("on"),
            powerW = o.optDouble("power_w", 0.0),
            energyKwh = o.optDouble("energy_kwh", 0.0),
            voltage = o.optDoubleOrNull("voltage"),
            currentA = o.optDoubleOrNull("current_a"),
            rssi = if (o.isNull("rssi")) null else o.optInt("rssi"),
            outlets = outlets,
        )
    }

    private fun JSONObject.optDoubleOrNull(key: String): Double? =
        if (isNull(key)) null else optDouble(key)
}
