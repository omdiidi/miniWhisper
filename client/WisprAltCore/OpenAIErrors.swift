import Foundation

/// How an OpenAI HTTP error is handled. Only `.rateLimited` and `.server`
/// are transient (worth one retry); the rest are permanent.
public enum OpenAIErrorKind: Equatable, Sendable {
    case auth, quota, modelNotFound, badRequest, rateLimited, server, other

    public var isTransient: Bool { self == .rateLimited || self == .server }

    public var failureKind: FailureKind {
        switch self {
        case .auth: return .auth
        case .quota: return .quota
        case .modelNotFound: return .modelNotFound
        case .rateLimited, .server: return .server
        case .badRequest, .other: return .other
        }
    }
}

public enum OpenAIErrors {
    private struct Envelope: Decodable {
        struct Detail: Decodable { let code: String?; let type: String? }
        let error: Detail?
    }

    /// Classifies a non-2xx response. The body is read only for the `code` /
    /// `type` fields and is never returned or logged.
    public static func kind(status: Int, body: Data?) -> OpenAIErrorKind {
        let detail = body.flatMap { try? JSONDecoder().decode(Envelope.self, from: $0) }?.error
        let codes = [detail?.code, detail?.type].compactMap { $0 }
        if codes.contains("insufficient_quota") { return .quota }
        if codes.contains("model_not_found") || status == 404 { return .modelNotFound }
        switch status {
        case 401, 403: return .auth
        case 400: return .badRequest
        case 429: return .rateLimited
        case 500...599: return .server
        default: return .other
        }
    }

    /// True for errors worth one retry: a transient HTTP kind, or a transport
    /// `URLError` other than a cancel.
    public static func isTransient(_ error: Error) -> Bool {
        if case ProviderError.http(_, let kind) = error { return kind.isTransient }
        if let url = error as? URLError { return url.code != .cancelled }
        return false
    }
}
