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

    public init(text: String, modelUsed: String, outputTokens: Int? = nil, formatting: Formatting = .off) {
        self.text = text
        self.modelUsed = modelUsed
        self.outputTokens = outputTokens
        self.formatting = formatting
    }
}

/// An OpenAI dictation provider: one fresh instance per dictation. The Local
/// server path is not a provider; it stays in the app.
public protocol DictationProvider: AnyObject {
    func finish(_ recording: Recording) async throws -> DictationResult
    func cancel()
}

/// A transcript is possibly cut off only when the model has a verified
/// output-token ceiling and the reply used all of it.
public enum TruncationCheck {
    public static func isTruncated(outputTokens: Int?, verifiedCap: Int?) -> Bool {
        guard let outputTokens, let verifiedCap else { return false }
        return outputTokens >= verifiedCap
    }
}
