import Foundation

/// Pure constants and rules shared by the client's file-job paths.
public enum FileJobContract {
    /// Server-side `failed` reason for a job the user cancelled
    /// (`DELETE /transcribe/file/{id}` → 202, then the runner ends it).
    /// A job that fails with exactly this reason is a silent end, not an error.
    public static let cancelledByUserReason = "cancelled by user"

    /// Formats fetched when a Local job is done: the server's `outputs` keys,
    /// sorted, or all four when it listed none. Used by the normal completion
    /// path and by restart recovery alike.
    public static func formatsToDownload(_ outputs: [String]) -> [String] {
        outputs.isEmpty ? ["json", "srt", "vtt", "txt"] : outputs.sorted()
    }

    /// Moves staged output files into a folder that `makeDirectory` creates,
    /// all or nothing: if any step throws, the new folder is removed and the
    /// error rethrown, so a failure never leaves a half-written job folder.
    @discardableResult
    public static func commitOutputs(staged: [URL], makeDirectory: () throws -> URL,
                                     move: (URL, URL) throws -> Void, remove: (URL) -> Void) throws -> URL {
        let dir = try makeDirectory()
        do {
            for file in staged { try move(file, dir.appendingPathComponent(file.lastPathComponent)) }
        } catch {
            remove(dir)
            throw error
        }
        return dir
    }
}

// MARK: - Saved Local job output

/// Where a Local file job's outputs go (`<outputDirectory>/<stem>.<fmt>`),
/// saved beside its job id so restart recovery downloads them to the same
/// place. Stored on this Mac only (UserDefaults), never logged.
public struct SavedJobOutput: Codable, Equatable, Sendable {
    public let outputDirectory: String
    public let stem: String

    public init(outputDirectory: URL, stem: String) {
        self.outputDirectory = outputDirectory.path
        self.stem = stem
    }

    public var directoryURL: URL { URL(fileURLWithPath: outputDirectory, isDirectory: true) }

    public func encoded() throws -> Data { try JSONEncoder().encode(self) }

    /// nil for missing or unreadable data (e.g. an id saved by an older build).
    public static func decode(_ data: Data?) -> SavedJobOutput? {
        guard let data, let out = try? JSONDecoder().decode(SavedJobOutput.self, from: data),
              !out.outputDirectory.isEmpty, !out.stem.isEmpty else { return nil }
        return out
    }
}

// MARK: - Online file job output

/// The `<stem>.json` an Online file job writes (TXT + JSON only; OpenAI
/// returns no timestamps, so there are no segments, SRT or VTT).
public struct OnlineTranscript: Codable, Equatable, Sendable {
    public struct Chunk: Codable, Equatable, Sendable {
        public let index: Int
        public let start_s: Double
        public let end_s: Double
        public let text: String

        public init(index: Int, start_s: Double, end_s: Double, text: String) {
            self.index = index
            self.start_s = start_s
            self.end_s = end_s
            self.text = text
        }
    }

    public var version = 1
    public var mode = "online"
    public let model: String
    public let source: String
    public let created_at: String
    public let duration_s: Double?
    public let text: String
    public let chunks: [Chunk]

    /// A direct upload is one chunk; `text` is the chunk texts joined by a space.
    public init(model: String, source: String, createdAt: Date, durationSeconds: Double?, chunks: [Chunk]) {
        self.model = model
        self.source = source
        self.created_at = ISO8601DateFormatter().string(from: createdAt)
        self.duration_s = durationSeconds
        self.chunks = chunks
        self.text = chunks.map { $0.text.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }.joined(separator: " ")
    }

    public func jsonData() throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try e.encode(self)
    }
}

// MARK: - Online upload plan

/// Whether an Online file job sends the picked file as is or decodes and
/// chunks it. Direct only for an OpenAI-accepted format, ≤ 25,000,000 bytes
/// and a known duration ≤ the file model's cap (A11: unknown → decode).
public enum OnlineUploadPlan: Equatable, Sendable {
    case direct(contentType: String)
    case decode

    static let contentTypes: [String: String] = [
        "mp3": "audio/mpeg", "mp4": "audio/mp4", "m4a": "audio/mp4", "wav": "audio/wav", "webm": "audio/webm",
        "ogg": "audio/ogg", "flac": "audio/flac", "mpeg": "audio/mpeg", "mpga": "audio/mpeg",
    ]

    public static func decide(ext: String, bytes: Int, durationSeconds: Double?, capSeconds: Int) -> OnlineUploadPlan {
        guard let type = contentTypes[ext.lowercased()], bytes <= OpenAIModels.uploadLimitBytes,
              let d = durationSeconds, d <= Double(capSeconds) else { return .decode }
        return .direct(contentType: type)
    }
}

// MARK: - Audio extraction policy

public enum ExtractError: LogSafeError, Equatable, Sendable {
    case noAudioTrack, exportFailed

    public var logDescription: String { self == .noAudioTrack ? "extract(noAudioTrack)" : "extract(exportFailed)" }
    public var outcomeKind: ErrorOutcome { .failed(.other) }
}

/// What `AudioExtractor` returns. Non-strict (Local) degrades to the original
/// on every failure, as before; strict (Online) fails instead, and keeps an
/// extracted file even when it is not smaller (the original may not decode).
public enum AudioExtractPolicy: Equatable, Sendable {
    case useOriginal, useExtracted, fail(ExtractError)

    public static func decide(strict: Bool, hasVideo: Bool, hasAudio: Bool, exportOK: Bool,
                              outBytes: Int, inBytes: Int) -> AudioExtractPolicy {
        guard hasVideo else { return .useOriginal }
        guard hasAudio else { return strict ? .fail(.noAudioTrack) : .useOriginal }
        guard exportOK, outBytes > 0 else { return strict ? .fail(.exportFailed) : .useOriginal }
        if outBytes < inBytes { return .useExtracted }
        return strict ? .useExtracted : .useOriginal
    }
}
