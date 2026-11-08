import Foundation

/// What a failed step means for the dictation that ran it.
public enum ErrorOutcome: Equatable, Sendable { case empty, cancelled, failed(FailureKind) }

public enum FailureKind: Equatable, Sendable {
    case keyMissing, auth, quota, modelNotFound, network, server,
         deadline, deviceChanged, writeFailed, encodeFailed, other
}

/// An error that knows how to describe itself to a log without transcript
/// text, response bodies, server reasons, paths, URLs or keys.
public protocol LogSafeError: Error {
    var logDescription: String { get }
    var outcomeKind: ErrorOutcome { get }
}

/// The only way an `Error` reaches a log line or a dictation outcome.
public enum ErrorLog {
    /// A `LogSafeError` describes itself; anything else is reduced to its type,
    /// domain and code (never `localizedDescription`, which can carry text).
    public static func describe(_ e: Error) -> String {
        if let safe = e as? LogSafeError { return safe.logDescription }
        let ns = e as NSError
        return "\(type(of: e)) \(ns.domain)#\(ns.code)"
    }

    public static func outcome(_ e: Error) -> ErrorOutcome {
        if let safe = e as? LogSafeError { return safe.outcomeKind }
        return e is CancellationError ? .cancelled : .failed(.other)
    }
}

/// Failures raised by the OpenAI dictation providers.
public enum ProviderError: LogSafeError, Equatable {
    case keyMissing
    case deadlineExceeded
    case emptyRecording
    case cancelled
    case http(status: Int, kind: OpenAIErrorKind)

    public var logDescription: String {
        switch self {
        case .keyMissing: return "keyMissing"
        case .deadlineExceeded: return "deadlineExceeded"
        case .emptyRecording: return "emptyRecording"
        case .cancelled: return "cancelled"
        case .http(let status, let kind): return "http(status: \(status), kind: \(kind))"
        }
    }

    public var outcomeKind: ErrorOutcome {
        switch self {
        case .keyMissing: return .failed(.keyMissing)
        case .deadlineExceeded: return .failed(.deadline)
        case .emptyRecording: return .empty
        case .cancelled: return .cancelled
        case .http(_, let kind): return .failed(kind.failureKind)
        }
    }
}
