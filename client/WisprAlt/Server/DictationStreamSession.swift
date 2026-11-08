import Foundation
import WisprAltCore

/// Failures surfaced by the streaming dictation path. The caller
/// (`DictationAPI.transcribe`) treats any of these as a signal to fall back
/// to the non-streaming origin retry path (origin, wait, origin), threading
/// the session's `clientDedupId` through so the retry row collides with the
/// streaming row on the server-side partial unique index (no duplicate).
enum StreamingFailure: Error {
    case aborted
    case serverRejected(code: Int)
    case decode(Error)
    case transport(Error)
}

/// Per-recording streaming dictation session: an actor wrapping the
/// chunk-by-chunk POST loop. Constructed by `DictationRecorder` at `start()`
/// only for a Local capture (a Local endpoint was given); handed off by
/// `stop()` inside `LocalCapture`; spilled to the recorder's tap
/// callback which calls `enqueueChunk(_:index:)` once per VAD-cut segment;
/// finally drained by `DictationAPI.transcribe(capture:)` via
/// `finalize(tail:smartFormat:)` which awaits all in-flight chunk POSTs and
/// then POSTs the trailing audio to the finalize endpoint.
///
/// Concurrency notes
/// -----------------
/// - `enqueueChunk(_:index:)` is intentionally NON-async so the recorder's
///   `chunkEncodeQueue` worker can `DispatchGroup.wait()` on a thin
///   `Task { await session.enqueueChunk(...) }` wrapper. By the time the
///   `await` returns to that wrapper, the actor isolation guarantee means
///   `inFlight[index]` has already been written — which is what the
///   recorder's group-wait actually synchronizes on.
/// - `abort()` cancels every in-flight chunk POST and latches `aborted`
///   so further enqueues / finalize calls short-circuit.
/// - The actor isolation domain is the synchronization primitive — no
///   manual locks are needed.
actor DictationStreamSession {

    // MARK: - Identity

    /// Server-side session id. Lower-cased UUID for the URL path; the
    /// server's `_UUID_RE` matches both cases but lower-case keeps log
    /// grepping uniform with the rest of the codebase.
    let sessionId: String = UUID().uuidString.lowercased()

    /// The dictation id (captured at FN press), sent as `X-Client-Dedup-Id`
    /// on every chunk and finalize and as the finalize `client_dedup_id`
    /// field. The whole-file fallback reuses it, so the server keeps exactly
    /// one row per dictation.
    let clientDedupId: String

    /// The Local server captured at FN press. A later Settings change never
    /// moves an in-flight dictation to another host.
    let endpoint: ServerEndpoint

    /// Wall-clock timestamp the recorder began capturing audio. Sent on
    /// `/finalize` as `speech_started_at` (Double seconds-since-epoch with
    /// sub-second precision) so the server can backfill `dictations.created_at`
    /// with the speech-start moment rather than the finalize moment.
    let speechStartedAt: Date

    // MARK: - State

    private var inFlight: [Int: Task<Void, Error>] = [:]
    private var aborted: Bool = false
    /// A chunk got a terminal 410/409 (`LocalDictationError`): finalize
    /// rethrows it so the caller does not fall back to the whole file.
    private var terminal: LocalDictationError?

    /// 8 s timeout per chunk POST — `STREAMING_CHUNK_POST_TIMEOUT_S` in the
    /// brief. If a chunk POST hangs longer than this the URLSession
    /// machinery surfaces a `URLError.timedOut`, which becomes
    /// `StreamingFailure.transport(...)` and triggers fallback.
    private static let chunkPostTimeoutS: TimeInterval = 8.0

    init(endpoint: ServerEndpoint, dictationId: String, speechStartedAt: Date) {
        self.endpoint = endpoint
        self.clientDedupId = dictationId.lowercased()
        self.speechStartedAt = speechStartedAt
    }

    // MARK: - Enqueue

    /// Spawns a detached Task that POSTs the chunk to
    /// `/transcribe/dictate/stream/{sessionId}/chunk/{index}`.
    ///
    /// SYNCHRONOUS in actor isolation: by the time `await` returns to the
    /// caller, inFlight[index] has been written. This is the registration
    /// synchronization the recorder's DispatchGroup.wait() depends on.
    func enqueueChunk(_ wav: Data, index: Int) {
        guard !aborted else { return }
        let t = Task.detached(priority: .userInitiated) { [self] in
            do {
                try await self.postChunk(wav, index: index)
                // Success: drop the finished task so it is not retained for
                // the rest of the session. Failures stay out of this path:
                // they latch `aborted`, which makes finalize() throw.
                await self.chunkFinished(index)
            } catch {
                await self.markAborted(cause: ErrorLog.describe(error), terminal: error as? LocalDictationError)
                throw error
            }
        }
        inFlight[index] = t
    }

    private func chunkFinished(_ index: Int) {
        inFlight[index] = nil
    }

    // MARK: - Finalize

    /// Awaits every in-flight chunk POST, then POSTs the trailing tail audio
    /// to `/transcribe/dictate/stream/{sessionId}/finalize` and returns the
    /// joined transcribed text (+ the Part 4 `saved` flag). Any chunk failure
    /// cancels the remaining in-flight tasks and rethrows so the caller can
    /// fall back; a terminal `LocalDictationError` is rethrown as is.
    func finalize(tail: Data, smartFormat: Bool) async throws -> LocalRequests.Response {
        if let terminal { throw terminal }
        guard !aborted else { throw StreamingFailure.aborted }
        // Release consumed tasks on every exit path; the session is one-shot.
        defer { inFlight.removeAll() }
        return try await withThrowingTaskGroup(of: Void.self) { group in
            for t in inFlight.values {
                group.addTask { _ = try await t.value }
            }
            do {
                try await group.waitForAll()
            } catch {
                group.cancelAll()
                self.markAborted(cause: "chunk_failed")
                throw error
            }
            return try await self.postFinalize(tail: tail, smartFormat: smartFormat)
        }
    }

    // MARK: - Abort

    /// Cancels every in-flight chunk POST and latches the session as
    /// aborted. Idempotent.
    func abort() {
        aborted = true
        for t in inFlight.values { t.cancel() }
        inFlight.removeAll()
    }

    /// `cause` is a fixed label or `ErrorLog.describe` output, never raw error text.
    private func markAborted(cause: String, terminal: LocalDictationError? = nil) {
        if let terminal, self.terminal == nil { self.terminal = terminal }
        if aborted { return }
        aborted = true
        for t in inFlight.values { t.cancel() }
        Log.warning(
            "stream session aborted: \(cause) sessionId=\(sessionId)",
            category: "dictation"
        )
    }

    // MARK: - Chunk POST

    private func postChunk(_ wav: Data, index: Int) async throws {
        if aborted { throw StreamingFailure.aborted }
        var request = LocalRequests.chunk(endpoint: endpoint, sessionId: sessionId, index: index,
                                          dedupId: clientDedupId, wav: wav, clientVersion: ServerClient.clientVersion)
        request.timeoutInterval = Self.chunkPostTimeoutS
        _ = try await send(request, label: "chunk \(index)")
        Log.info(
            "stream chunk \(index) → 202 sessionId=\(sessionId) bytes=\(wav.count)",
            category: "streaming"
        )
    }

    /// POSTs `request`; a non-2xx throws the terminal `LocalDictationError`
    /// (410 `dictation_deleted` / 409 `dedup_conflict`) or `serverRejected`.
    private func send(_ request: URLRequest, label: String) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw StreamingFailure.transport(error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw StreamingFailure.transport(URLError(.badServerResponse))
        }
        guard (200...299).contains(http.statusCode) else {
            Log.warning(
                "stream \(label) → HTTP \(http.statusCode) sessionId=\(sessionId)",
                category: "dictation"
            )
            if let terminal = LocalRequests.terminalError(status: http.statusCode, body: data) { throw terminal }
            throw StreamingFailure.serverRejected(code: http.statusCode)
        }
        return data
    }

    // MARK: - Finalize POST

    private func postFinalize(tail: Data, smartFormat: Bool) async throws -> LocalRequests.Response {
        if aborted { throw StreamingFailure.aborted }
        let request = LocalRequests.finalize(
            endpoint: endpoint, sessionId: sessionId, dedupId: clientDedupId, speechStartedAt: speechStartedAt,
            tail: tail, smartFormat: smartFormat, clientVersion: ServerClient.clientVersion
        )
        let data = try await send(request, label: "finalize")
        let decoded: LocalRequests.Response
        do {
            decoded = try LocalRequests.decode(data)
        } catch {
            throw StreamingFailure.decode(error)
        }
        Log.info(
            "stream finalize → 200 sessionId=\(sessionId) chars=\(decoded.text.count) saved=\(decoded.saved)",
            category: "streaming"
        )
        return decoded
    }
}
