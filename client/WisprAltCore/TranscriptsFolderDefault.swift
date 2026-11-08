import Foundation

/// Picks the folder where file transcripts are saved.
public enum TranscriptsFolderDefault {
    /// A stored (non-empty) path always wins. With nothing stored, an existing
    /// `<docs>/WisprAlt/Meetings` (the meeting-era default) is kept so existing
    /// users' transcripts stay where they are; otherwise the default is
    /// `<docs>/WisprAlt/Transcripts`. The caller pins the result so the folder
    /// never moves later.
    public static func resolve(stored: String?, docs: URL, exists: (URL) -> Bool) -> URL {
        if let stored, !stored.isEmpty {
            return URL(fileURLWithPath: stored, isDirectory: true)
        }
        let base = docs.appendingPathComponent("WisprAlt", isDirectory: true)
        let meetings = base.appendingPathComponent("Meetings", isDirectory: true)
        if exists(meetings) { return meetings }
        return base.appendingPathComponent("Transcripts", isDirectory: true)
    }
}
