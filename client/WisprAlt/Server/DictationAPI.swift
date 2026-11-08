import Foundation
import WisprAltCore

/// Namespace for the dictation transcription endpoint.
///
/// Calls `POST /transcribe/dictate` on the configured server URL. If the
/// first attempt fails with an offline signature (per
/// `ServerClient.isOfflineSignature`), it waits `retrySpacingSec` and tries
/// the origin once more. If the origin is still offline, the error is
/// thrown and the caller surfaces the usual dictation error toast. There is
/// no cloud audio fallback.
///
/// When a `clientDedupId` is threaded through (streaming finalize failed),
/// both origin requests carry `X-Client-Dedup-Id` so the server's dedup
/// index collapses a duplicate row.
enum DictationAPI {
    // MARK: - Response model

    private struct TranscribeResponse: Decodable {
        let text: String
        let model_id: String
        let duration_ms: Double
        let smart_formatted: Bool?
    }

    /// Spacing between origin retry attempts before giving up on the mini.
    private static let retrySpacingSec: TimeInterval = 5

    // MARK: - Public API

    /// Transcribes a finished Local capture and returns the text.
    ///
    /// `LocalFinalizeRoute` picks the path: when at least one chunk went out
    /// (`armed`) the streaming session is finalized by POSTing the tail WAV to
    /// `/transcribe/dictate/stream/{sessionId}/finalize`, after awaiting every
    /// pending chunk POST. Otherwise the whole WAV goes to `/transcribe/dictate`.
    ///
    /// If the streaming finalize fails for ANY reason (chunk POST error,
    /// finalize 5xx, timeout, decode error, abort) the session is aborted and
    /// the whole WAV takes the origin retry path (origin, wait, origin) with
    /// the session's `clientDedupId`, so the retry row collides with any
    /// already-persisted streaming row on the server's partial unique index:
    /// exactly one `dictations` row per logical dictation.
    static func transcribe(capture: LocalCapture) async throws -> String {
        guard LocalFinalizeRoute.decide(armed: capture.armed, hasSession: capture.session != nil) == .streamFinalize,
              let session = capture.session
        else {
            return try await runExistingPath(wavData: capture.wholeWAV, clientDedupId: nil)
        }
        let dedupId = session.clientDedupId
        do {
            return try await session.finalize(tail: capture.tailWAV, smartFormat: Settings.shared.smartFormatting)
        } catch {
            Log.warning(
                "DictationAPI: streaming failed (\(ErrorLog.describe(error))); falling back with dedup=\(dedupId).",
                category: "dictation"
            )
            await session.abort()
            return try await runExistingPath(wavData: capture.wholeWAV, clientDedupId: dedupId)
        }
    }

    /// Non-streaming path: origin attempt, wait, origin retry. Threads
    /// `clientDedupId` into the `X-Client-Dedup-Id` header when non-nil
    /// (server dedup contract).
    private static func runExistingPath(
        wavData: Data,
        clientDedupId: String?
    ) async throws -> String {
        let boundary = UUID().uuidString
        let body = buildMultipartBody(wavData: wavData, boundary: boundary)

        var additionalHeaders: [String: String] = [:]
        if Settings.shared.smartFormatting {
            additionalHeaders["X-Smart-Format"] = "true"
        }
        if let id = clientDedupId {
            additionalHeaders["X-Client-Dedup-Id"] = id
        }

        // Attempt 1 — origin.
        let attempt1 = await runOriginAttempt(body: body, boundary: boundary, additionalHeaders: additionalHeaders)
        switch attempt1.result {
        case .success(let text):
            return text
        case .failure(let err, let serverAttempt):
            if !ServerClient.shared.isOfflineSignature(serverAttempt) {
                throw err
            }
            Log.info("DictationAPI: origin attempt 1 → offline signature; retrying once.", category: "dictation")
        }

        // Attempt 2 — origin retry.
        try await Task.sleep(nanoseconds: UInt64(retrySpacingSec * 1_000_000_000))
        let attempt2 = await runOriginAttempt(body: body, boundary: boundary, additionalHeaders: additionalHeaders)
        switch attempt2.result {
        case .success(let text):
            return text
        case .failure(let err, let serverAttempt):
            if !ServerClient.shared.isOfflineSignature(serverAttempt) {
                throw err
            }
            Log.warning("DictationAPI: origin attempt 2 → offline confirmed; giving up.", category: "dictation")
            throw err
        }
    }

    // MARK: - Origin attempt

    private struct AttemptResult {
        enum Outcome {
            case success(String)
            case failure(Error, ServerClient.RequestAttempt)
        }
        let result: Outcome
    }

    private static func runOriginAttempt(
        body: Data,
        boundary: String,
        additionalHeaders: [String: String]
    ) async -> AttemptResult {
        let startedAt = Date()
        do {
            let request = try ServerClient.shared.buildRequest(
                path: "/transcribe/dictate",
                method: "POST",
                body: body,
                contentType: "multipart/form-data; boundary=\(boundary)",
                additionalHeaders: additionalHeaders
            )
            let (data, http) = try await ServerClient.shared.execute(request, retryOnReset: true)
            let decoded = try JSONDecoder().decode(TranscribeResponse.self, from: data)
            Log.debug(
                "Dictation response: \"\(decoded.text)\" in \(decoded.duration_ms)ms via \(decoded.model_id) (status=\(http.statusCode))",
                category: "dictation"
            )
            return AttemptResult(result: .success(decoded.text))
        } catch {
            let finishedAt = Date()
            let attempt = makeAttempt(error: error, startedAt: startedAt, finishedAt: finishedAt)
            return AttemptResult(result: .failure(mapDecodeError(error), attempt))
        }
    }

    // MARK: - Classifier helpers

    private static func makeAttempt(
        error: Error,
        startedAt: Date,
        finishedAt: Date
    ) -> ServerClient.RequestAttempt {
        let outcome: ServerClient.RequestAttempt.Outcome
        if let urlErr = error as? URLError {
            outcome = .error(urlErr)
        } else if case ServerError.transport(let underlying) = error {
            outcome = .error(underlying)
        } else if case ServerError.server(let status, _) = error,
                  let synthetic = syntheticResponse(status: status)
        {
            outcome = .response(synthetic)
        } else {
            outcome = .error(error)
        }
        return ServerClient.RequestAttempt(
            startedAt: startedAt,
            finishedAt: finishedAt,
            lastByteSentAt: nil,
            outcome: outcome
        )
    }

    private static func syntheticResponse(status: Int) -> HTTPURLResponse? {
        guard let url = Settings.shared.serverURL else { return nil }
        return HTTPURLResponse(
            url: url,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["X-Request-Id": "synthetic"]
        )
    }

    private static func mapDecodeError(_ error: Error) -> Error {
        if error is DecodingError {
            return ServerError.decoding(error)
        }
        return error
    }

    // MARK: - Multipart body

    private static func buildMultipartBody(wavData: Data, boundary: String) -> Data {
        var body = Data()
        let crlf = "\r\n"
        body.append("--\(boundary)\(crlf)".utf8Data)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\(crlf)".utf8Data)
        body.append("Content-Type: audio/wav\(crlf)".utf8Data)
        body.append(crlf.utf8Data)
        body.append(wavData)
        body.append(crlf.utf8Data)
        body.append("--\(boundary)--\(crlf)".utf8Data)
        return body
    }
}

// MARK: - String → Data helper

private extension String {
    var utf8Data: Data {
        Data(utf8)
    }
}

// MARK: - LastDictation (v0.5.0)

/// Response shape for `GET /me/dictations/last`.
struct LastDictation: Decodable {
    let id: String
    let text: String
    let created_at: Double  // epoch seconds (SQLite REAL → JSON number)
}

/// Namespace for the `/me/dictations/last` endpoint used by the menubar
/// "Copy last dictation" button. Matches the MeAPI.get() pattern exactly:
/// build via `ServerClient.shared.buildRequest`, execute via
/// `ServerClient.shared.execute`, decode with JSONDecoder.
enum LastDictationAPI {
    /// `GET /me/dictations/last` — fetch the caller's most recent
    /// (non-deleted) dictation. Throws `ServerError.server(status: 404, _)`
    /// when the user has zero dictations.
    static func fetch() async throws -> LastDictation {
        let request = try ServerClient.shared.buildRequest(
            path: "/me/dictations/last",
            method: "GET"
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        do {
            return try JSONDecoder().decode(LastDictation.self, from: data)
        } catch {
            throw ServerError.decoding(error)
        }
    }
}
