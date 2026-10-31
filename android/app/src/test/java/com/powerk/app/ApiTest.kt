package com.powerk.app

import com.sun.net.httpserver.HttpServer
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.net.InetSocketAddress

/**
 * Wire-level check of the JSON client, run on the JVM by `gradlew testDebugUnitTest`.
 * Catches the classic HttpURLConnection mistake: setting doOutput without writing the body.
 */
class ApiTest {

    private class Captured {
        var body = ""
        var token = ""
        var method = ""
        var contentType = ""
    }

    private fun serve(responseBody: String, captured: Captured, status: Int = 200): Pair<HttpServer, String> {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/api/onoff") { exchange ->
            captured.method = exchange.requestMethod
            captured.token = exchange.requestHeaders.getFirst("X-Token") ?: ""
            captured.contentType = exchange.requestHeaders.getFirst("Content-Type") ?: ""
            captured.body = exchange.requestBody.readBytes().toString(Charsets.UTF_8)
            val bytes = responseBody.toByteArray()
            exchange.sendResponseHeaders(status, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
        server.start()
        return server to "http://127.0.0.1:${server.address.port}"
    }

    @Test
    fun postSendsJsonBodyAndToken() {
        val captured = Captured()
        val (server, base) = serve("""{"ok":true,"confirmed":true}""", captured)
        try {
            val body = JSONObject()
                .put("mac", "88D0391C0C4C")
                .put("outlet", 2)
                .put("on", true)
                .toString()
            val response = Api.request("$base/api/onoff", body, "sekrit")

            assertTrue("response: $response", JSONObject(response).optBoolean("ok"))
            assertEquals("POST", captured.method)
            assertEquals("sekrit", captured.token)
            assertTrue(captured.contentType.startsWith("application/json"))
            val sent = JSONObject(captured.body)
            assertEquals("88D0391C0C4C", sent.getString("mac"))
            assertEquals(2, sent.getInt("outlet"))
            assertTrue(sent.getBoolean("on"))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun outletsAreLookedUpByNumberNotListPosition() {
        val outlets = listOf(
            Outlet(n = 1, on = true, powerW = 1.0, energyKwh = 0.1, tempC = 30),
            Outlet(n = 2, on = false, powerW = 2.0, energyKwh = 0.2, tempC = 31),
            Outlet(n = 3, on = false, powerW = 3.0, energyKwh = 0.3, tempC = 32),
            Outlet(n = 4, on = true, powerW = 4.0, energyKwh = 0.4, tempC = 33),
        )
        val strip = Strip(
            mac = "88D0391C0C4C", name = "LG+ Power", model = "lgutap", fw = "0.1.54-1.0.66",
            online = true, on = true, powerW = 10.0, energyKwh = 1.0,
            voltage = 221.0, currentA = 0.1, rssi = -53, outlets = outlets,
        )

        // outlet 4 is at list index 3: indexing the list by outlet number used to throw
        assertTrue(strip.outletOn(1))
        assertTrue(strip.outletOn(4))
        assertEquals(33, strip.outlet(4)?.tempC)
        assertEquals(null, strip.outlet(5))
        assertTrue(!strip.outletOn(5))
    }

    @Test
    fun errorResponsesBecomeServerExceptions() {
        val captured = Captured()
        val (server, base) = serve("""{"error":"device offline"}""", captured, 503)
        try {
            try {
                Api.request("$base/api/onoff", "{}", "")
                fail("HTTP 503 must throw")
            } catch (e: ServerException) {
                assertEquals("device offline", e.message)
            }
            assertEquals("", captured.token)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun nonJsonErrorsKeepTheHttpStatus() {
        val (server, base) = serve("token required", Captured(), 401)
        try {
            try {
                Api.request("$base/api/onoff", "{}", "wrong")
                fail("HTTP 401 must throw")
            } catch (e: ServerException) {
                assertEquals("HTTP 401", e.message)
            }
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun redirectsDoNotForwardTokens() {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        var forwarded = false
        server.createContext("/redirect") { exchange ->
            exchange.responseHeaders.add("Location", "/target")
            exchange.sendResponseHeaders(302, -1)
            exchange.close()
        }
        server.createContext("/target") { exchange ->
            forwarded = true
            exchange.sendResponseHeaders(200, -1)
            exchange.close()
        }
        server.start()
        try {
            try {
                Api.request("http://127.0.0.1:${server.address.port}/redirect", null, "sekrit")
                fail("Redirect must not be followed")
            } catch (e: ServerException) {
                assertEquals("HTTP 302", e.message)
            }
            assertTrue(!forwarded)
        } finally {
            server.stop(0)
        }
    }
}
