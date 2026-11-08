import Foundation

/// A finished recording on disk, handed to a provider after FN-up.
public struct Recording: Sendable, Equatable {
    public let fileURL: URL
    public let durationSeconds: Double
    public let interruptedByDeviceChange: Bool

    public init(fileURL: URL, durationSeconds: Double, interruptedByDeviceChange: Bool) {
        self.fileURL = fileURL
        self.durationSeconds = durationSeconds
        self.interruptedByDeviceChange = interruptedByDeviceChange
    }
}

public enum Formatting: Equatable, Sendable { case off, applied, skipped(String), failed(String) }

public struct DictationResult: Equatable, Sendable {
    public var text: String
    /// The model that produced `text` (metadata; safe to log).
    public var modelUsed: String
    public var outputTokens: Int?
    public var formatting: Formatting
    /// Local only: false when the server returned the text but could not
    /// store it (Part 4 `"saved": false`). The text is delivered as usual;
    /// Task 7 keeps the audio for Retry. Always true for Online.
    public var serverSaved: Bool

    public init(text: String, modelUsed: String, outputTokens: Int? = nil, formatting: Formatting = .off,
                serverSaved: Bool = true) {
        self.text = text
        self.modelUsed = modelUsed
        self.outputTokens = outputTokens
        self.formatting = formatting
        self.serverSaved = serverSaved
    }
}

/// An OpenAI dictation provider: one fresh instance per dictation. The Local
/// server path is not a provider; it stays in the app.
public protocol DictationProvider: AnyObject {
    func finish(_ recording: Recording) async throws -> DictationResult
    func cancel()
}

/// A provider that takes audio while FN is held (the Realtime live models).
/// The recorder feeds `append` mono Float samples at `inputSampleRate` from
/// one serial queue; `start()` is called once, right after capture starts.
public protocol LiveFrameSink: AnyObject {
    var inputSampleRate: Int { get }
    func start()
    func append(_ mono: [Float])
}

/// A transcript is possibly cut off only when the model has a verified
/// output-token ceiling and the reply used all of it.
public enum TruncationCheck {
    public static func isTruncated(outputTokens: Int?, verifiedCap: Int?) -> Bool {
        guard let outputTokens, let verifiedCap else { return false }
        return outputTokens >= verifiedCap
    }
}
