package com.powerk.app

/** Immutable request identity. Never publish a response belonging to another connection. */
data class ServerConnection(val base: String, val token: String, val revision: Int = 0) {
    fun isCurrent(current: ServerConnection): Boolean = this == current
}

/** Pure guard used by UI loaders; callback receives the exact credentials captured at request start. */
internal suspend fun <T> connectionResult(connection: ServerConnection,
    current: () -> ServerConnection, fetch: suspend (ServerConnection) -> T): T? {
    val value = fetch(connection)
    return if (connection.isCurrent(current())) value else null
}
