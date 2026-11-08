import Foundation
import WisprAltCore

/// The Local server dictation path. Not a Core `DictationProvider`: Local
/// needs the app-only `LocalCapture` (streaming session + native-rate WAVs).
/// Bound to the endpoint and dictation id captured at FN press, so a later
/// Settings change never moves an in-flight dictation.
struct LocalServerProvider {
    let endpoint: ServerEndpoint
    let dictationId: String

    /// Finalize (or whole file) under `max(30, 10 + 0.5 × duration)`. On
    /// timeout the streaming session is aborted and the work is cancelled,
    /// which cancels the in-flight finalize / whole-file `URLSession` request.
    func finish(capture: LocalCapture, durationSeconds: Double) async throws -> DictationResult {
        let session = capture.session
        let (endpoint, dedupId) = (endpoint, dictationId)
        let response = try await withDeadline(
            seconds: max(30, 10 + 0.5 * durationSeconds),
            onTimeout: { [session] in Task { await session?.abort() } }
        ) {
            try await DictationAPI.transcribe(capture: capture, endpoint: endpoint, dedupId: dedupId)
        }
        return DictationResult(text: response.text, modelUsed: "local", serverSaved: response.saved)
    }

    /// Retry of a saved recording: the whole file to this endpoint with the
    /// entry's dictation id as `X-Client-Dedup-Id`, under the same deadline.
    func retry(wav: Data, durationSeconds: Double) async throws -> DictationResult {
        let (endpoint, dedupId) = (endpoint, dictationId)
        let response = try await withDeadline(seconds: max(30, 10 + 0.5 * durationSeconds), onTimeout: {}) {
            try await DictationAPI.wholeFile(endpoint: endpoint, wavData: wav, dedupId: dedupId)
        }
        return DictationResult(text: response.text, modelUsed: "local", serverSaved: response.saved)
    }
}
