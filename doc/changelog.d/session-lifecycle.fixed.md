Reconnect cannot replace an existing Fluent session. Server lifecycle routing is
serialized, worker operations share a lock with synchronous shutdown, and canceled
calls retain their lock until the worker actually finishes. Connection status
checks Fluent's live is_active API and endpoint metadata describes the actual
connection. Unexpected tool errors retain details in server logs instead of
exposing internal exception messages and tracebacks to clients.
