package com.powerk.app

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class ServerConnectionTest {
    @Test fun delayedOldServerSnapshotCannotReplaceCurrentServer() = runBlocking {
        val old = ServerConnection("http://a:8080", "a-token", 0)
        var current = old
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val load = async {
            connectionResult(old, { current }) { request ->
                assertEquals(old, request)
                started.complete(Unit)
                release.await()
                Snapshot("a", emptyList())
            }
        }
        started.await()
        current = ServerConnection("http://b:8080", "b-token", 1)
        release.complete(Unit)
        assertNull(load.await())
    }
    @Test fun delayedOldServerSettingsCannotPopulateNewServerEditor() = runBlocking {
        val old = ServerConnection("http://a:8080", "", 0)
        var current = old
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val load = async {
            connectionResult(old, { current }) {
                started.complete(Unit); release.await()
                ServerSettings(currency = "OLD", priceKwh = 100.0)
            }
        }
        started.await()
        current = ServerConnection("http://b:8080", "", 1)
        release.complete(Unit)
        assertNull(load.await())
    }
    @Test fun connectionRevisionRejectsResponsesAfterSwitchingAwayAndBack() = runBlocking {
        val old = ServerConnection("http://a:8080", "", 0)
        val again = old.copy(revision = 2)
        assertNull(connectionResult(old, { again }) { "stale" })
    }
    @Test fun unchangedConnectionAcceptsLastGoodResponse() = runBlocking {
        val connection = ServerConnection("http://a:8080", "token", 1)
        assertEquals("good", connectionResult(connection, { connection }) { "good" })
        assertFalse(connection.isCurrent(connection.copy(token = "other")))
    }
}
