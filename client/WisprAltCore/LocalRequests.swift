import Foundation

/// Builds the Local server dictation requests and reads their responses.
/// Every request goes to the endpoint captured at FN press (never a global),
/// with `Authorization`, `X-WisprAlt-Client-Version` and the lowercase
/// `X-Client-Dedup-Id` (the dictation id; Part 4 dedup contract, A5).
public enum LocalRequests {
    /// `POST /transcribe/dictate/stream/{sessionId}/chunk/{index}` with one `file` part.
    public static func chunk(endpoint: ServerEndpoint, sessionId: String, index: Int, dedupId: String,
                             wav: Data, clientVersion: String) -> URLRequest {
        let form = Multipart(file: ("chunk.wav", wav))
        return post(endpoint, "/transcribe/dictate/stream/\(sessionId)/chunk/\(index)", dedupId, clientVersion, form)
    }

    /// `POST /transcribe/dictate/stream/{sessionId}/finalize` with `file`,
    /// `client_dedup_id`, `speech_started_at` (seconds since 1970, Double) and
    /// `smart_format`.
    public static func finalize(endpoint: ServerEndpoint, sessionId: String, dedupId: String, speechStartedAt: Date,
                                tail: Data, smartFormat: Bool, clientVersion: String) -> URLRequest {
        let form = Multipart(file: ("tail.wav", tail), fields: [
            ("smart_format", smartFormat ? "true" : "false"),
            ("client_dedup_id", dedupId.lowercased()),
            ("speech_started_at", "\(speechStartedAt.timeIntervalSince1970)"),
        ])
        return post(endpoint, "/transcribe/dictate/stream/\(sessionId)/finalize", dedupId, clientVersion, form)
    }

    /// `POST /transcribe/dictate` with the whole recording (also the streaming
    /// fallback and, later, Retry). Smart formatting rides on `X-Smart-Format`.
    public static func wholeFile(endpoint: ServerEndpoint, dedupId: String, wav: Data, smartFormat: Bool,
                                 clientVersion: String) -> URLRequest {
        var request = post(endpoint, "/transcribe/dictate", dedupId, clientVersion, Multipart(file: ("audio.wav", wav)))
        if smartFormat { request.setValue("true", forHTTPHeaderField: "X-Smart-Format") }
        return request
    }

    // MARK: - Responses

    /// A 200 dictation response. `saved == false` (Part 4) means the server
    /// returned the text but could not store it: deliver the text, keep the audio.
    public struct Response: Decodable, Equatable, Sendable {
        public let text: String
        public let saved: Bool

        public init(text: String, saved: Bool) {
            self.text = text
            self.saved = saved
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            text = try c.decode(String.self, forKey: .text)
            saved = try c.decodeIfPresent(Bool.self, forKey: .saved) ?? true   // pre-Part 4 servers
        }

        private enum CodingKeys: String, CodingKey { case text, saved }
    }

    public static func decode(_ data: Data) throws -> Response {
        try JSONDecoder().decode(Response.self, from: data)
    }

    /// 410 `{"error":"dictation_deleted"}` and 409 `{"error":"dedup_conflict"}`
    /// are terminal for this dictation id. A 409/410 without that `error`
    /// (e.g. a stream route's `{"detail": …}`) is not.
    public static func terminalError(status: Int, body: Data) -> LocalDictationError? {
        guard status == 409 || status == 410,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              let code = json["error"] as? String
        else { return nil }
        switch (status, code) {
        case (410, "dictation_deleted"): return .dictationDeleted
        case (409, "dedup_conflict"): return .dedupConflict
        default: return nil
        }
    }

    // MARK: - Private

    private struct Multipart {
        var file: (name: String, data: Data)
        var fields: [(String, String)] = []
        let boundary = "wispralt-\(UUID().uuidString)"

        var body: Data {
            var body = Data()
            for (name, value) in fields {
                body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
            }
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(file.name)\"\r\n".utf8))
            body.append(Data("Content-Type: audio/wav\r\n\r\n".utf8))
            body.append(file.data)
            body.append(Data("\r\n--\(boundary)--\r\n".utf8))
            return body
        }
    }

    private static func post(_ endpoint: ServerEndpoint, _ path: String, _ dedupId: String,
                             _ clientVersion: String, _ form: Multipart) -> URLRequest {
        let url = URL(string: path, relativeTo: endpoint.baseURL)?.absoluteURL
            ?? endpoint.baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        if let key = endpoint.apiKey { request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
        request.setValue(clientVersion, forHTTPHeaderField: "X-WisprAlt-Client-Version")
        request.setValue(dedupId.lowercased(), forHTTPHeaderField: "X-Client-Dedup-Id")
        request.setValue("multipart/form-data; boundary=\(form.boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = form.body
        return request
    }
}

/// The Local server refused this dictation id for good (Part 4 contract):
/// never retried, its Retry entry and audio are discarded (Task 7).
public enum LocalDictationError: LogSafeError, Equatable {
    case dictationDeleted, dedupConflict

    public var logDescription: String {
        switch self {
        case .dictationDeleted: return "server(status: 410, dictation_deleted)"
        case .dedupConflict: return "server(status: 409, dedup_conflict)"
        }
    }

    public var outcomeKind: ErrorOutcome { .failed(.rejected) }
}
