import Foundation

/// How a dictation ended, as far as its spooled audio is concerned.
public enum DictationOutcome: Equatable, Sendable {
    /// Text came back. `serverSaved == false` is a Local 200 with `"saved": false`;
    /// `textSaved == false` means `last.json` could not be written.
    case text(truncated: Bool, serverSaved: Bool, textSaved: Bool)
    case emptyText, emptyRecording
    case failed(FailureKind)

    /// `.empty` → `.emptyRecording`; `.cancelled` → `.failed(.other)` (audio kept).
    public init(_ outcome: ErrorOutcome) {
        switch outcome {
        case .empty: self = .emptyRecording
        case .cancelled: self = .failed(.other)
        case .failed(let kind): self = .failed(kind)
        }
    }
}

/// What `TextInjector` did with the text.
public enum InjectOutcome: Equatable, Sendable {
    /// AX insert read back as landed; clipboard untouched.
    case insertedVerified
    /// Cmd+V sent; the text stays on the clipboard (never restored over).
    case pastedUnverified
    /// Secure field: nothing typed, nothing copied (TextInjector shows its notice).
    case refusedSecureField
    /// No frontmost app: copied only.
    case noTarget
}

public enum SpoolToast: Equatable, Sendable {
    case copiedNotPasted, possiblyTruncated, notSavedOnServer, lastTextNotSaved, noSpeech
    case failed(FailureKind)
}

public struct SpoolDecision: Equatable, Sendable {
    /// true → move the audio to `retry/`; false → delete it.
    public let keep: Bool
    public let toasts: [SpoolToast]

    public init(keep: Bool, toasts: [SpoolToast] = []) {
        self.keep = keep
        self.toasts = toasts
    }
}

/// Keep or delete the spooled audio once text exists and delivery was tried.
public enum SpoolDisposition {
    /// The one keep rule for a recording whose text came back, shared by the
    /// normal dictation path (`decide`) and Retry (`RetryEntries.dropAfterRetry`):
    /// keep it while the text may be cut off, the server did not store it, or
    /// `last.json` could not be written (the clipboard alone is not a copy).
    public static func keepsAudio(truncated: Bool, serverSaved: Bool, textSaved: Bool) -> Bool {
        truncated || !serverSaved || !textSaved
    }

    public static func decide(_ outcome: DictationOutcome, delivery: InjectOutcome?) -> SpoolDecision {
        switch outcome {
        case .emptyRecording:
            return SpoolDecision(keep: false)
        case .emptyText:
            // Audio exists but no words came back: keep it so nothing said is lost.
            return SpoolDecision(keep: true, toasts: [.noSpeech])
        case .failed(.rejected):
            // Terminal (410 dictation_deleted / 409 dedup_conflict): never retried.
            return SpoolDecision(keep: false, toasts: [.failed(.rejected)])
        case .failed(let kind):
            return SpoolDecision(keep: true, toasts: [.failed(kind)])
        case .text(let truncated, let serverSaved, let textSaved):
            guard let delivery else { return SpoolDecision(keep: true) }   // text never reached delivery
            var toasts: [SpoolToast] = []
            if truncated { toasts.append(.possiblyTruncated) }
            if !serverSaved { toasts.append(.notSavedOnServer) }
            if !textSaved { toasts.append(.lastTextNotSaved) }
            if delivery == .noTarget { toasts.append(.copiedNotPasted) }
            return SpoolDecision(keep: keepsAudio(truncated: truncated, serverSaved: serverSaved, textSaved: textSaved),
                                 toasts: toasts)
        }
    }
}
