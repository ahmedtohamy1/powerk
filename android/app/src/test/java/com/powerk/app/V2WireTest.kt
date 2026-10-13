package com.powerk.app

import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.net.InetSocketAddress

class V2WireTest {
    private data class Request(val method: String, val path: String, val query: String?,
        val body: String, val contentType: String?, val token: String?)
    private fun withServer(status: Int = 200, response: String = """{"ok":true,"confirmed":false,"rule":{},"events":[]}""",
        test: (String, MutableList<Request>) -> Unit) {
        val requests = mutableListOf<Request>()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/api/") { e ->
            requests += Request(e.requestMethod, e.requestURI.path, e.requestURI.query,
                e.requestBody.readBytes().toString(Charsets.UTF_8), e.requestHeaders.getFirst("Content-Type"),
                e.requestHeaders.getFirst("X-Token"))
            val bytes = response.toByteArray()
            e.sendResponseHeaders(status, bytes.size.toLong())
            e.responseBody.use { it.write(bytes) }
        }
        server.start()
        try { test("http://127.0.0.1:${server.address.port}", requests) }
        finally { server.stop(0) }
    }
    @Test fun everyV2MutationUsesCorrectMethodAuthAndJsonBody() = withServer { base, requests ->
        runBlocking {
            Api.rename(base, "secret", "ABC", 4, "Fridge")
            Api.createRule(base, "secret", JSONObject().put("kind", "timer"))
            Api.updateRule(base, "secret", "r_abc", JSONObject().put("enabled", false))
            Api.saveSettings(base, "secret", JSONObject().put("budget", JSONObject.NULL))
            Api.readEvents(base, "secret", 4102444800L)
            Api.testAlerts(base, "secret")
            assertFalse(Api.setOutlet(base, "secret", "ABC", 4, false))
            Api.deleteRule(base, "secret", "r_abc")
        }
        assertEquals(listOf("POST", "POST", "PUT", "PUT", "POST", "POST", "POST", "DELETE"), requests.map { it.method })
        requests.forEach { r ->
            assertEquals("secret", r.token)
            if (r.method != "DELETE") {
                assertTrue(r.contentType.orEmpty().startsWith("application/json"))
                JSONObject(r.body)
            } else {
                assertEquals("", r.body)
                assertNull(r.contentType)
            }
        }
        assertEquals(4, JSONObject(requests[0].body).getInt("outlet"))
        assertEquals("Fridge", JSONObject(requests[0].body).getString("name"))
        assertFalse(JSONObject(requests[2].body).getBoolean("enabled"))
        assertEquals(4102444800L, JSONObject(requests[4].body).getLong("through_id"))
        assertEquals("{}", requests[5].body)
        assertFalse(JSONObject(requests[6].body).getBoolean("on"))
    }
    @Test fun queriesAndOptionalFiltersMatchTheContract() = withServer { base, requests ->
        runBlocking {
            Api.snapshot(base, "")
            Api.rules(base, "", "ABC")
            Api.rules(base, "")
            Api.history(base, "", "ABC", "week", 4)
            Api.settings(base, "")
            Api.events(base, "", 4102444800L)
            Api.readEvents(base, "")
        }
        assertEquals("/api/state", requests[0].path)
        assertEquals("mac=ABC", requests[1].query)
        assertNull(requests[2].query)
        assertEquals("mac=ABC&range=week&outlet=4", requests[3].query)
        assertEquals("/api/settings", requests[4].path)
        assertEquals("limit=50&before_id=4102444800", requests[5].query)
        assertEquals("{}", requests[6].body)
        requests.take(6).forEach { assertEquals("GET", it.method); assertEquals("", it.body); assertNull(it.token) }
    }
    @Test fun actual400ResponsePreservesServerValidationMessage() = withServer(400, """{"error":"minutes must be 1..10080"}""") { base, _ ->
        runBlocking {
            try { Api.createRule(base, "", JSONObject()); fail("expected 400") }
            catch (e: ServerException) { assertEquals(400, e.status); assertEquals("minutes must be 1..10080", e.message) }
        }
    }
    @Test fun missingV1EndpointsPreserve404ForFriendlyUpdateMessage() = withServer(404, """{"error":"unknown path"}""") { base, _ ->
        runBlocking {
            try { Api.rules(base, ""); fail("expected 404") }
            catch (e: ServerException) { assertEquals(404, e.status) }
        }
    }
    @Test fun failedUnknownCommandIsNeverRetried() = withServer(502, """{"error":"outcome unknown"}""") { base, requests ->
        runBlocking {
            try { Api.setOutlet(base, "", "ABC", 4, true); fail("expected 502") }
            catch (e: ServerException) { assertEquals(502, e.status) }
        }
        assertEquals(1, requests.size)
    }
}
