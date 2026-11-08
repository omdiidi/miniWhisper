import Foundation
import WisprAltCore

/// Namespace for the Local dictation endpoints, bound to the endpoint
/// captured at FN press (never `Settings.serverURL`).
///
/// Every request carries `X-Client-Dedup-Id: <dictation id>` (built by Core
/// `LocalRequests`), so the server keeps one row per dictation however many
/// paths it took. If the whole-file attempt fails with an offline signature
/// (per `ServerClient.isOfflineSignature`) it waits `retrySpacingSec` and
/// tries once more, then throws. There is no cloud audio fallback.
///
/// A 410 `dictation_deleted` / 409 `dedup_conflict` is terminal
/// (`LocalDictationError`): no fallback, no retry.
enum DictationAPI {
    /// Spacing between origin retry attempts before giving up on the mini.
    private static let retrySpacingSec: TimeInterval = 5

    // MARK: - Public API

    /// Transcribes a finished Local capture.
    ///
    /// `LocalFinalizeRoute` picks the path: when at least one chunk went out
    /// (`armed`) the streaming session is finalized by POSTing the tail WAV to
    /// `/transcribe/dictate/stream/{sessionId}/finalize`, after awaiting every
    /// pending chunk POST. Otherwise the whole WAV goes to `/transcribe/dictate`.
    ///
    /// If the streaming finalize fails for any non-terminal reason (chunk POST
    /// error, finalize 5xx, timeout, decode error, abort) the session is
    /// aborted and the whole WAV takes the origin retry path with the same
    /// dedup id, so the server's dedup index collapses any duplicate row.
    static func transcribe(capture: LocalCapture, endpoint: ServerEndpoint,
                           dedupId: String) async throws -> LocalRequests.Response {
        guard LocalFinalizeRoute.decide(armed: capture.armed, hasSession: capture.session != nil) == .streamFinalize,
              let session = capture.session
        else {
            return try await wholeFile(endpoint: endpoint, wavData: capture.wholeWAV, dedupId: dedupId)
        }
        do {
            return try await session.finalize(tail: capture.tailWAV, smartFormat: Settings.shared.smartFormatting)
        } catch let terminal as LocalDictationError {
            await session.abort()
            throw terminal
        } catch {
            Log.warning(
                "DictationAPI: streaming failed (\(ErrorLog.describe(error))); falling back to the whole file.",
                category: "dictation"
            )
            await session.abort()
            try Task.checkCancellation()
            return try await wholeFile(endpoint: endpoint, wavData: capture.wholeWAV, dedupId: dedupId)
        }
    }

    /// Non-streaming path (also Retry): origin attempt, wait, origin retry.
    static func wholeFile(
        endpoint: ServerEndpoint,
        wavData: Data,
        dedupId: String
    ) async throws -> LocalRequests.Response {
        let request = LocalRequests.wholeFile(endpoint: endpoint, dedupId: dedupId, wav: wavData,
                                              smartFormat: Settings.shared.smartFormatting,
                                              clientVersion: ServerClient.clientVersion)

        // Attempt 1 — origin.
        let attempt1 = await runOriginAttempt(request, endpoint: endpoint)
        switch attempt1.result {
        case .success(let response):
            return response
        case .failure(let err, let serverAttempt):
            if !ServerClient.shared.isOfflineSignature(serverAttempt) {
                throw err
            }
            Log.info("DictationAPI: origin attempt 1 → offline signature; retrying once.", category: "dictation")
        }

        // Attempt 2 — origin retry.
        try await Task.sleep(nanoseconds: UInt64(retrySpacingSec * 1_000_000_000))
        let attempt2 = await runOriginAttempt(request, endpoint: endpoint)
        switch attempt2.result {
        case .success(let response):
            return response
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
            case success(LocalRequests.Response)
            case failure(Error, ServerClient.RequestAttempt)
        }
        let result: Outcome
    }

    private static func runOriginAttempt(_ request: URLRequest, endpoint: ServerEndpoint) async -> AttemptResult {
        let startedAt = Date()
        do {
            let (data, http) = try await ServerClient.shared.execute(request, retryOnReset: true)
            let decoded = try LocalRequests.decode(data)
            Log.debug(
                "Dictation response: chars=\(decoded.text.count) saved=\(decoded.saved) (status=\(http.statusCode))",
                category: "dictation"
            )
            return AttemptResult(result: .success(decoded))
        } catch {
            let attempt = makeAttempt(error: error, endpoint: endpoint, startedAt: startedAt, finishedAt: Date())
            return AttemptResult(result: .failure(mapDecodeError(error), attempt))
        }
    }

    // MARK: - Classifier helpers

    private static func makeAttempt(
        error: Error,
        endpoint: ServerEndpoint,
        startedAt: Date,
        finishedAt: Date
    ) -> ServerClient.RequestAttempt {
        let outcome: ServerClient.RequestAttempt.Outcome
        if let urlErr = error as? URLError {
            outcome = .error(urlErr)
        } else if case ServerError.transport(let underlying) = error {
            outcome = .error(underlying)
        } else if case ServerError.server(let status, _) = error,
                  let synthetic = syntheticResponse(status: status, url: endpoint.baseURL)
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

    private static func syntheticResponse(status: Int, url: URL) -> HTTPURLResponse? {
        HTTPURLResponse(
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
}
