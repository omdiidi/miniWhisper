import Foundation

/// Failures surfaced by the streaming dictation path. The caller
/// (`DictationAPI.transcribe`) treats any of these as a signal to fall back
/// to the standard 3-attempt origin → OpenRouter ladder, threading the
/// session's `clientDedupId` through so the fallback row collides with the
/// streaming row on the server-side partial unique index (no duplicate).
enum StreamingFailure: Error {
    case aborted
    case serverRejected(code: Int)
    case decode(Error)
    case transport(Error)
}

/// Per-recording streaming dictation session: an actor wrapping the
/// chunk-by-chunk POST loop. Constructed by `DictationRecorder` at `start()`
/// when `Settings.streamingDictation == true`; spilled to the recorder's tap
/// callback which calls `enqueueChunk(_:index:)` once per VAD-cut segment;
/// finally drained by `DictationAPI.transcribe(_:recorder:)` via
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

    /// Client-supplied dedup id sent as a multipart form field on
    /// `/finalize`. Also threaded into the existing `/transcribe/dictate`
    /// ladder via `X-Client-Dedup-Id` when the streaming path fails so the
    /// fallback row collides with the streaming row at server-side ingest.
    let clientDedupId: String = UUID().uuidString.lowercased()

    /// Wall-clock timestamp the recorder began capturing audio. Sent on
    /// `/finalize` as `speech_started_at` (Double seconds-since-epoch with
    /// sub-second precision) so the server can backfill `dictations.created_at`
    /// with the speech-start moment rather than the finalize moment.
    let speechStartedAt: Date

    // MARK: - State

    private var inFlight: [Int: Task<Void, Error>] = [:]
    private var aborted: Bool = false

    /// 8 s timeout per chunk POST — `STREAMING_CHUNK_POST_TIMEOUT_S` in the
    /// brief. If a chunk POST hangs longer than this the URLSession
    /// machinery surfaces a `URLError.timedOut`, which becomes
    /// `StreamingFailure.transport(...)` and triggers fallback.
    private static let chunkPostTimeoutS: TimeInterval = 8.0

    init(speechStartedAt: Date) {
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
            } catch {
                await self.markAborted(reason: "\(error)")
                throw error
            }
        }
        inFlight[index] = t
    }

    // MARK: - Finalize

    /// Awaits every in-flight chunk POST, then POSTs the trailing tail audio
    /// to `/transcribe/dictate/stream/{sessionId}/finalize` and returns the
    /// joined transcribed text. Any chunk failure cancels the remaining
    /// in-flight tasks and rethrows so the caller can fall back.
    func finalize(tail: Data, smartFormat: Bool) async throws -> String {
        guard !aborted else { throw StreamingFailure.aborted }
        return try await withThrowingTaskGroup(of: Void.self) { group in
            for t in inFlight.values {
                group.addTask { _ = try await t.value }
            }
            do {
                try await group.waitForAll()
            } catch {
                group.cancelAll()
                self.markAborted(reason: "chunk_failed")
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
    }

    private func markAborted(reason: String) {
        if aborted { return }
        aborted = true
        for t in inFlight.values { t.cancel() }
        Log.warning(
            "stream session aborted: \(reason) sessionId=\(sessionId)",
            category: "fallback"
        )
    }

    // MARK: - Chunk POST

    private func postChunk(_ wav: Data, index: Int) async throws {
        if aborted { throw StreamingFailure.aborted }
        let boundary = UUID().uuidString
        let body = Self.buildChunkMultipartBody(wavData: wav, boundary: boundary)

        var request: URLRequest
        do {
            request = try ServerClient.shared.buildRequest(
                path: "/transcribe/dictate/stream/\(sessionId)/chunk/\(index)",
                method: "POST",
                body: body,
                contentType: "multipart/form-data; boundary=\(boundary)",
                additionalHeaders: [:]
            )
        } catch {
            throw StreamingFailure.transport(error)
        }
        request.timeoutInterval = Self.chunkPostTimeoutS

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw StreamingFailure.transport(error)
        }
        _ = data
        guard let http = response as? HTTPURLResponse else {
            throw StreamingFailure.transport(URLError(.badServerResponse))
        }
        if !(200...299).contains(http.statusCode) {
            Log.warning(
                "stream chunk \(index) → HTTP \(http.statusCode) sessionId=\(sessionId)",
                category: "fallback"
            )
            throw StreamingFailure.serverRejected(code: http.statusCode)
        }
        Log.info(
            "stream chunk \(index) → 202 sessionId=\(sessionId) bytes=\(wav.count)",
            category: "streaming"
        )
    }

    // MARK: - Finalize POST

    private struct FinalizeResponse: Decodable {
        let text: String
        let model_id: String
        let duration_ms: Double
        let smart_formatted: Bool?
    }

    private func postFinalize(tail: Data, smartFormat: Bool) async throws -> String {
        if aborted { throw StreamingFailure.aborted }
        let boundary = UUID().uuidString
        let body = Self.buildFinalizeMultipartBody(
            tailWav: tail,
            smartFormat: smartFormat,
            clientDedupId: clientDedupId,
            speechStartedAt: speechStartedAt,
            boundary: boundary
        )

        let request: URLRequest
        do {
            request = try ServerClient.shared.buildRequest(
                path: "/transcribe/dictate/stream/\(sessionId)/finalize",
                method: "POST",
                body: body,
                contentType: "multipart/form-data; boundary=\(boundary)",
                additionalHeaders: [:]
            )
        } catch {
            throw StreamingFailure.transport(error)
        }

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
        if !(200...299).contains(http.statusCode) {
            Log.warning(
                "stream finalize → HTTP \(http.statusCode) sessionId=\(sessionId)",
                category: "fallback"
            )
            throw StreamingFailure.serverRejected(code: http.statusCode)
        }
        let decoded: FinalizeResponse
        do {
            decoded = try JSONDecoder().decode(FinalizeResponse.self, from: data)
        } catch {
            throw StreamingFailure.decode(error)
        }
        Log.info(
            "stream finalize → 200 sessionId=\(sessionId) chars=\(decoded.text.count) " +
                "duration_ms=\(decoded.duration_ms) sf=\(decoded.smart_formatted ?? false)",
            category: "streaming"
        )
        return decoded.text
    }

    // MARK: - Multipart body builders

    /// Single `file` part — mirrors `DictationAPI.buildMultipartBody`.
    private static func buildChunkMultipartBody(wavData: Data, boundary: String) -> Data {
        var body = Data()
        let crlf = "\r\n"
        body.append("--\(boundary)\(crlf)".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"file\"; filename=\"chunk.wav\"\(crlf)"
                .data(using: .utf8)!
        )
        body.append("Content-Type: audio/wav\(crlf)".data(using: .utf8)!)
        body.append(crlf.data(using: .utf8)!)
        body.append(wavData)
        body.append(crlf.data(using: .utf8)!)
        body.append("--\(boundary)--\(crlf)".data(using: .utf8)!)
        return body
    }

    /// `file` + `smart_format` + `client_dedup_id` + `speech_started_at` parts.
    /// `speech_started_at` is the Double-precision seconds-since-epoch string;
    /// the server consumes it as `Form(float)` so sub-second precision is
    /// preserved end-to-end.
    private static func buildFinalizeMultipartBody(
        tailWav: Data,
        smartFormat: Bool,
        clientDedupId: String,
        speechStartedAt: Date,
        boundary: String
    ) -> Data {
        var body = Data()
        let crlf = "\r\n"

        // file
        body.append("--\(boundary)\(crlf)".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"file\"; filename=\"tail.wav\"\(crlf)"
                .data(using: .utf8)!
        )
        body.append("Content-Type: audio/wav\(crlf)".data(using: .utf8)!)
        body.append(crlf.data(using: .utf8)!)
        body.append(tailWav)
        body.append(crlf.data(using: .utf8)!)

        // smart_format
        body.append("--\(boundary)\(crlf)".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"smart_format\"\(crlf)\(crlf)"
                .data(using: .utf8)!
        )
        body.append("\(smartFormat ? "true" : "false")\(crlf)".data(using: .utf8)!)

        // client_dedup_id
        body.append("--\(boundary)\(crlf)".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"client_dedup_id\"\(crlf)\(crlf)"
                .data(using: .utf8)!
        )
        body.append("\(clientDedupId)\(crlf)".data(using: .utf8)!)

        // speech_started_at — Double-precision seconds-since-epoch
        body.append("--\(boundary)\(crlf)".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"speech_started_at\"\(crlf)\(crlf)"
                .data(using: .utf8)!
        )
        body.append("\(speechStartedAt.timeIntervalSince1970)\(crlf)".data(using: .utf8)!)

        body.append("--\(boundary)--\(crlf)".data(using: .utf8)!)
        return body
    }
}
