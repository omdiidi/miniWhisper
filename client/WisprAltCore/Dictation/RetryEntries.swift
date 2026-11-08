import Foundation

/// `spool/<id>.json` / `retry/<id>.json`: written before recording starts, so
/// a Retry entry always knows which mode and model it belongs to.
public struct SpoolSidecar: Codable, Equatable, Sendable {
    public let mode: TranscriptionMode
    /// OpenAI model id, or "local".
    public let model: String

    public init(mode: TranscriptionMode, model: String) {
        self.mode = mode
        self.model = model
    }

    public func encoded() throws -> Data { try JSONEncoder().encode(self) }
    public static func decode(_ data: Data?) -> SpoolSidecar? {
        data.flatMap { try? JSONDecoder().decode(SpoolSidecar.self, from: $0) }
    }
}

public enum RetryAction: Equatable, Sendable { case retry, export, discard }

/// The Retry list policy: newest 2, 7 days, no 0 s entries, no orphan sidecars.
public enum RetryEntries {
    public static let maxEntries = 2
    public static let maxAge: TimeInterval = 7 * 24 * 3600

    /// After a successful Retry: drop the entry only when the text reached
    /// `last.json` (`textSaved`), the server stored it and words came back
    /// (a failed save, empty text or `saved: false` keep the recording).
    public static func dropAfterRetry(_ result: DictationResult, textSaved: Bool) -> Bool {
        !SpoolDisposition.keepsAudio(truncated: false, serverSaved: result.serverSaved, textSaved: textSaved)
            && !result.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// One id found in `retry/` (audio and/or sidecar).
    public struct Entry: Equatable, Sendable {
        public let id: String
        public let createdAt: Date
        public let durationSeconds: Double
        public let hasAudio: Bool

        public init(id: String, createdAt: Date, durationSeconds: Double, hasAudio: Bool) {
            self.id = id
            self.createdAt = createdAt
            self.durationSeconds = durationSeconds
            self.hasAudio = hasAudio
        }
    }

    public struct Plan: Equatable, Sendable {
        /// Newest first.
        public let keep: [Entry]
        public let delete: [String]
        /// Valid entries dropped only because the list was full ("Oldest saved recording discarded").
        public let droppedForSpace: Int
    }

    public static func plan(_ entries: [Entry], now: Date) -> Plan {
        let valid = entries
            .filter { $0.hasAudio && $0.durationSeconds > 0 && now.timeIntervalSince($0.createdAt) <= maxAge }
            .sorted { $0.createdAt > $1.createdAt }
        let keep = Array(valid.prefix(maxEntries))
        let keepIDs = Set(keep.map(\.id))
        return Plan(keep: keep,
                    delete: entries.map(\.id).filter { !keepIDs.contains($0) },
                    droppedForSpace: valid.count - keep.count)
    }

    /// Valid sidecar of the current mode → Retry, Export…, Discard. Missing or
    /// corrupt → Export…, Discard. The other mode → Export…, Discard + a hint.
    public static func actions(sidecar: Data?, currentMode: TranscriptionMode) -> (actions: [RetryAction], hint: String?) {
        guard let s = SpoolSidecar.decode(sidecar) else { return ([.export, .discard], nil) }
        if s.mode == currentMode { return ([.retry, .export, .discard], nil) }
        return ([.export, .discard], s.mode == .local ? "Switch to Local to retry" : "Switch to Online to retry")
    }
}
