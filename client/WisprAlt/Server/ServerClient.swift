import Foundation

/// Central HTTP client for the WisprAlt server.
///
/// - `session` is used for short dictation requests (low latency, default configuration).
/// - `backgroundSession` is used for large meeting uploads (resumable, background transfer).
///
/// Error mapping:
///   - 401 → `.unauthorized`
///   - 413 → `.uploadTooLarge`
///   - 422 → `.uploadTruncated`
///   - 429 with body "meeting in progress" → `.meetingInProgress`
///   - other 429 → `.rateLimited(retryAfter:)`
///   - other 4xx/5xx → `.server(status:body:)`
///   - `URLError.networkConnectionLost` / `.timedOut` → one retry for dictation only.
///     Meeting uploads (large files) are NEVER retried blindly.
final class ServerClient {
    // MARK: - Singleton

    static let shared = ServerClient()

    // MARK: - Sessions

    /// Default session for dictation (small payloads, low latency).
    private let session: URLSession

    /// Background session for meeting uploads (resumable, survives app suspend).
    let backgroundSession: URLSession

    // MARK: - Init

    private init() {
        let defaultConfig = URLSessionConfiguration.default
        defaultConfig.timeoutIntervalForRequest = 30
        defaultConfig.timeoutIntervalForResource = 120
        self.session = URLSession(configuration: defaultConfig)

        let bgConfig = URLSessionConfiguration.background(
            withIdentifier: "co.wispralt.meeting-upload"
        )
        bgConfig.isDiscretionary = false
        bgConfig.sessionSendsLaunchEvents = true
        self.backgroundSession = URLSession(configuration: bgConfig)
    }

    // MARK: - Request builder

    /// Builds a `URLRequest` for the given path, method, and optional body.
    ///
    /// Reads `Settings.shared.serverURL` and `KeychainHelper.getAPIKey()`.
    /// Adds `Authorization: Bearer <key>` header when a key is available.
    ///
    /// - Parameters:
    ///   - path: Server path starting with `/`, e.g. `/healthz`.
    ///   - method: HTTP method. Defaults to `"GET"`.
    ///   - body: Optional request body data.
    ///   - contentType: Value for the `Content-Type` header. Ignored when `body` is nil.
    ///   - additionalHeaders: Any extra headers to merge in.
    /// - Throws: `ServerError.missingConfiguration` if `serverURL` is nil, or
    ///   `ServerError.invalidServerURL` if the URL cannot be constructed.
    func buildRequest(
        path: String,
        method: String = "GET",
        body: Data? = nil,
        contentType: String? = nil,
        additionalHeaders: [String: String] = [:]
    ) throws -> URLRequest {
        guard let baseURL = Settings.shared.serverURL else {
            throw ServerError.missingConfiguration
        }
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw ServerError.invalidServerURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = method

        // Bearer auth — tolerate missing key gracefully (health check doesn't need it).
        if let apiKey = try? KeychainHelper.getAPIKey(), let key = apiKey {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }

        if let body {
            request.httpBody = body
            if let ct = contentType {
                request.setValue(ct, forHTTPHeaderField: "Content-Type")
            }
        }

        for (field, value) in additionalHeaders {
            request.setValue(value, forHTTPHeaderField: field)
        }

        return request
    }

    // MARK: - Execute (with retry for dictation)

    /// Performs a data task, mapping HTTP errors to `ServerError`.
    ///
    /// Retries once on `URLError.networkConnectionLost` or `.timedOut` when
    /// `retryOnReset` is true (dictation only — never for meeting uploads).
    func execute(
        _ request: URLRequest,
        retryOnReset: Bool = false
    ) async throws -> (Data, HTTPURLResponse) {
        do {
            return try await performRequest(request)
        } catch let urlErr as URLError
            where retryOnReset && (urlErr.code == .networkConnectionLost || urlErr.code == .timedOut)
        {
            Log.warning(
                "URLError \(urlErr.code.rawValue) — retrying once.",
                category: "network"
            )
            return try await performRequest(request)
        }
    }

    // MARK: - Private helpers

    private func performRequest(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ServerError.transport(error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw ServerError.transport(URLError(.badServerResponse))
        }

        try mapHTTPError(status: http.statusCode, response: http, body: data)
        return (data, http)
    }

    /// Maps HTTP status codes to typed `ServerError` values.
    /// - Throws: the mapped `ServerError` when status indicates failure.
    func mapHTTPError(
        status: Int,
        response: HTTPURLResponse,
        body: Data
    ) throws {
        guard status >= 400 else { return }

        let bodyString = String(data: body, encoding: .utf8)

        switch status {
        case 401:
            throw ServerError.unauthorized
        case 413:
            throw ServerError.uploadTooLarge
        case 422:
            throw ServerError.uploadTruncated
        case 429:
            // Distinguish "meeting in progress" from generic rate limit.
            if let bodyStr = bodyString,
               bodyStr.lowercased().contains("meeting in progress")
            {
                throw ServerError.meetingInProgress
            }
            let retryAfter = response.value(forHTTPHeaderField: "Retry-After")
                .flatMap { TimeInterval($0) }
            throw ServerError.rateLimited(retryAfter: retryAfter)
        default:
            throw ServerError.server(status: status, body: bodyString)
        }
    }

    // MARK: - Health / readiness

    /// Calls `/healthz` (no auth required). Returns true if the server is reachable.
    func healthz() async throws -> Bool {
        let request = try buildRequest(path: "/healthz")
        // healthz needs no auth; build a bare request without bearer.
        var bareRequest = request
        bareRequest.setValue(nil, forHTTPHeaderField: "Authorization")
        let (_, http) = try await execute(bareRequest)
        return http.statusCode == 200
    }

    /// Calls `/readyz/{endpoint}` and returns the ok flag plus whether dictation is degraded.
    ///
    /// The `X-Dictation-Degraded: true` response header signals that a meeting job is currently
    /// consuming most server memory and dictation latency may be elevated.
    ///
    /// - Parameter endpoint: `"dictation"` or `"meeting"`.
    func readyz(endpoint: String) async throws -> (ok: Bool, degraded: Bool) {
        let request = try buildRequest(path: "/readyz/\(endpoint)")
        let (_, http) = try await execute(request)
        let ok = http.statusCode == 200
        let degraded = http.value(forHTTPHeaderField: "X-Dictation-Degraded") == "true"
        return (ok, degraded)
    }
}
