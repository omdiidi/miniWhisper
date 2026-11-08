import Foundation
import WisprAltCore

/// Typed errors from the WisprAlt server. Each case maps to a distinct HTTP condition
/// or transport failure so callers can handle them specifically.
enum ServerError: Error, LocalizedError {
    /// `Settings.shared.serverURL` is nil — user has not configured a server URL.
    case missingConfiguration

    /// The stored URL cannot be used to build a valid request (malformed).
    case invalidServerURL

    /// Server returned HTTP 401 — API key missing or wrong.
    case unauthorized

    /// Server returned HTTP 429 — too many requests.
    /// `retryAfter` is the value of the `Retry-After` header in seconds, if present.
    case rateLimited(retryAfter: TimeInterval?)

    /// Server returned HTTP 429 with `code=file_job_busy` — a file transcription job is
    /// already running (or still finishing after a cancel) and only one may run at a time.
    case fileJobInProgress

    /// Server returned HTTP 413 — the upload payload exceeded the server limit.
    case uploadTooLarge

    /// Server returned HTTP 422 — the upload was truncated or had an invalid WAV header.
    case uploadTruncated

    /// Server returned an unexpected 4xx or 5xx status.
    case server(status: Int, body: String?)

    /// Response JSON could not be decoded into the expected model type.
    case decoding(Error)

    /// A transport-level error (e.g. `URLError`). The underlying error is preserved for
    /// diagnostics.
    case transport(Error)

    // MARK: - LocalizedError

    var errorDescription: String? {
        switch self {
        case .missingConfiguration:
            return "No server URL is configured. Open Settings to set one."
        case .invalidServerURL:
            return "The configured server URL is invalid. Please check Settings."
        case .unauthorized:
            return "API key rejected by server. Verify your API key in Settings."
        case .rateLimited(let retryAfter):
            if let seconds = retryAfter {
                return String(format: "Too many requests. Retry in %.0f seconds.", seconds)
            }
            return "Too many requests. Please wait before trying again."
        case .fileJobInProgress:
            return "The previous file job is still finishing on the server. Wait for it to finish, then try again."
        case .uploadTooLarge:
            return "The file is too large for the server to accept. Check the server MAX_UPLOAD_BYTES setting."
        case .uploadTruncated:
            return "The upload appears incomplete or corrupted. Please try again."
        case .server(let status, _):
            // The body stays on the value for code paths; it never reaches
            // user-facing or log text.
            return "Server error \(status)."
        case .decoding(let underlying):
            return "Could not parse the server response: \(underlying.localizedDescription)"
        case .transport(let underlying):
            return "Network error: \(underlying.localizedDescription)"
        }
    }
}

/// Log text never includes the response body; only the status or error kind.
extension ServerError: LogSafeError {
    var logDescription: String {
        switch self {
        case .missingConfiguration: return "missingConfiguration"
        case .invalidServerURL: return "invalidServerURL"
        case .unauthorized: return "server(status: 401)"
        case .rateLimited: return "server(status: 429)"
        case .fileJobInProgress: return "fileJobInProgress"
        case .uploadTooLarge: return "server(status: 413)"
        case .uploadTruncated: return "server(status: 422)"
        case .server(let status, _): return "server(status: \(status))"
        case .decoding: return "decoding"
        case .transport(let underlying): return "transport(\(ErrorLog.describe(underlying)))"
        }
    }

    var outcomeKind: ErrorOutcome {
        switch self {
        case .unauthorized: return .failed(.auth)
        case .transport(let underlying):
            if let url = underlying as? URLError, url.code == .cancelled { return .cancelled }
            return .failed(.network)
        case .rateLimited, .fileJobInProgress, .server, .decoding: return .failed(.server)
        case .missingConfiguration, .invalidServerURL, .uploadTooLarge, .uploadTruncated: return .failed(.other)
        }
    }
}
