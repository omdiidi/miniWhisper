import Foundation

/// Where audio goes: OpenAI directly, or the user's own Local server.
public enum TranscriptionMode: String, Codable, Sendable, CaseIterable {
    case online, local

    /// Mode used when nothing is stored (fresh install and upgrade alike).
    public static let `default`: TranscriptionMode = .online
}

/// The Local server a dictation was captured against.
public struct ServerEndpoint: Sendable, Equatable {
    public let baseURL: URL
    public let apiKey: String?

    public init(baseURL: URL, apiKey: String?) {
        self.baseURL = baseURL
        self.apiKey = apiKey
    }
}

/// Which path a dictation takes, decided once at FN press.
public enum RouteKind: Equatable, Sendable {
    case needKey, openAIFile, openAIRealtime, local

    public static func decide(mode: TranscriptionMode, modelKind: OpenAIModel.Kind, keyPresent: Bool) -> RouteKind {
        switch mode {
        case .local: return .local
        case .online:
            guard keyPresent else { return .needKey }
            return modelKind == .live ? .openAIRealtime : .openAIFile
        }
    }
}

/// Everything a dictation needs, frozen at FN press. A later mode or model
/// change never alters it, so the recording finishes on the route it started on.
public struct DictationSnapshot: Equatable, Sendable {
    public let id: String
    public let mode: TranscriptionMode
    public let kind: RouteKind
    /// The OpenAI model; nil for Local.
    public let model: OpenAIModel?
    /// The Local server; nil for every non-Local route.
    public let endpoint: ServerEndpoint?

    public init(id: String = UUID().uuidString.lowercased(), mode: TranscriptionMode, model: OpenAIModel,
                keyPresent: Bool, endpoint: ServerEndpoint?) {
        self.id = id
        self.mode = mode
        self.kind = RouteKind.decide(mode: mode, modelKind: model.kind, keyPresent: keyPresent)
        self.model = kind == .local ? nil : model
        self.endpoint = kind == .local ? endpoint : nil
    }

    /// The endpoint Local streaming may use. Only a Local route ever gets one.
    public var streamEndpoint: ServerEndpoint? { kind == .local ? endpoint : nil }

    public var capSeconds: Int { model?.dictationCapSeconds ?? OpenAIModels.maxDictationSeconds }
}

// MARK: - Mode switch / launch actions

public enum ModeEvent: Equatable, Sendable { case launch, switched(to: TranscriptionMode), onlineJobEnded }

/// Inputs to `LaunchActions.decide`. `pollerRunning` is "a Local poll task
/// exists"; `onlineJobRunning` is "an Online file job task exists".
public struct LocalJobState: Equatable, Sendable {
    public var mode: TranscriptionMode
    public var hasServerConfig: Bool
    public var hasSavedJobID: Bool
    public var pollerRunning: Bool
    public var onlineJobRunning: Bool
    public var meFetchedThisLaunch: Bool

    public init(mode: TranscriptionMode, hasServerConfig: Bool, hasSavedJobID: Bool,
                pollerRunning: Bool, onlineJobRunning: Bool, meFetchedThisLaunch: Bool) {
        self.mode = mode
        self.hasServerConfig = hasServerConfig
        self.hasSavedJobID = hasSavedJobID
        self.pollerRunning = pollerRunning
        self.onlineJobRunning = onlineJobRunning
        self.meFetchedThisLaunch = meFetchedThisLaunch
    }
}

/// What to do with the Local job poller and `/me` at launch, on a mode switch,
/// and when an Online file job ends. Nothing here ends a recording or touches
/// an Online job, and Online never reaches the Local host.
public struct LaunchActions: Equatable, Sendable {
    public let resumeLocalJob: Bool
    public let fetchMe: Bool
    public let cancelLocalPoll: Bool

    public init(resumeLocalJob: Bool = false, fetchMe: Bool = false, cancelLocalPoll: Bool = false) {
        self.resumeLocalJob = resumeLocalJob
        self.fetchMe = fetchMe
        self.cancelLocalPoll = cancelLocalPoll
    }

    public static func decide(_ event: ModeEvent, _ s: LocalJobState) -> LaunchActions {
        let canResume = s.mode == .local && s.hasServerConfig && s.hasSavedJobID
            && !s.pollerRunning && !s.onlineJobRunning
        let canFetchMe = s.mode == .local && s.hasServerConfig && !s.meFetchedThisLaunch
        switch event {
        case .launch, .switched(to: .local):
            return LaunchActions(resumeLocalJob: canResume, fetchMe: canFetchMe)
        case .switched(to: .online):
            return LaunchActions(cancelLocalPoll: s.pollerRunning)
        case .onlineJobEnded:
            return LaunchActions(resumeLocalJob: canResume)
        }
    }
}

/// Generation fence for the Local job wrapper and poller: a write is allowed
/// only from a live task whose captured generation is still current.
public enum PollFence {
    public static func mayWrite(isCancelled: Bool, captured: Int, current: Int) -> Bool {
        !isCancelled && captured == current
    }
}

// MARK: - Activity state (menubar icon)

/// Phase of the Local file job, for the icon and the mode picker lock.
public enum LocalUploadPhase: Equatable, Sendable {
    case none, converting, uploading, polling

    /// Before the server has a job id, a switch would orphan the upload.
    public var locksModePicker: Bool { self == .converting || self == .uploading }
}

public enum ActivityIcon: Equatable, Sendable { case idle, dictating, converting, uploading, processing, done }

/// Derives the menubar icon from state. The icon is never the state machine.
public enum ActivityState {
    public static func icon(dictating: Bool, local: LocalUploadPhase, onlineJob: Bool, doneFlash: Bool) -> ActivityIcon {
        if dictating { return .dictating }
        switch local {
        case .converting: return .converting
        case .uploading: return .uploading
        case .polling: return .processing
        case .none: break
        }
        if onlineJob { return .processing }
        return doneFlash ? .done : .idle
    }
}
