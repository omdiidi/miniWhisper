import Foundation

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

    /// Server returned HTTP 429 with body "meeting in progress" — a meeting job is already
    /// running on the server and only one may run at a time.
    case meetingInProgress

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
        case .meetingInProgress:
            return "A meeting is already being processed on the server. Wait for it to finish before submitting a new recording."
        case .uploadTooLarge:
            return "The recording is too large for the server to accept. Check the server MAX_UPLOAD_BYTES setting."
        case .uploadTruncated:
            return "The upload appears incomplete or corrupted. Please try recording again."
        case .server(let status, let body):
            if let body {
                return "Server error \(status): \(body)"
            }
            return "Server error \(status)."
        case .decoding(let underlying):
            return "Could not parse the server response: \(underlying.localizedDescription)"
        case .transport(let underlying):
            return "Network error: \(underlying.localizedDescription)"
        }
    }
}
