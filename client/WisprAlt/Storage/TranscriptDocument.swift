import Foundation

// MARK: - Errors

/// Typed errors for transcript document operations.
enum TranscriptError: Error, LocalizedError {
    /// The `rawKey` is not present in `speakers`.
    case unknownSpeaker(String)
    /// The requested `newName` is already used as a display name by another speaker.
    case speakerNameConflict(String)
    /// A file system I/O error occurred.
    case ioError(Error)
    /// The JSON file could not be decoded.
    case decodingError(Error)

    var errorDescription: String? {
        switch self {
        case .unknownSpeaker(let key):
            return "No speaker found with raw key "\(key)"."
        case .speakerNameConflict(let name):
            return "The name "\(name)" is already used by another speaker."
        case .ioError(let underlying):
            return "File I/O error: \(underlying.localizedDescription)"
        case .decodingError(let underlying):
            return "Could not decode transcript: \(underlying.localizedDescription)"
        }
    }
}

// MARK: - Document model

/// Full in-memory representation of a WisprAlt meeting transcript.
///
/// Matches the locked v3 JSON schema. The `speakers` dictionary is keyed by
/// `speaker_raw` (the stable pyannote label or `"mic"`) and each entry holds
/// the current `display_name`. `segments` carry both `speaker_raw` (immutable)
/// and `speaker` (denormalised current display name, rewritten on rename).
///
/// Speaker rename is entirely client-side — no server round-trip. Use
/// `renameSpeaker(rawKey:to:)` which validates collision and updates both the
/// `speakers` table and all matching segments atomically (in memory).
struct TranscriptDocument: Codable {
    // MARK: - Nested types

    struct Word: Codable {
        let word: String
        let start: Double?
        let end: Double?
        let score: Double?
    }

    struct Segment: Codable {
        var start: Double
        var end: Double
        var channel: Int?

        /// Current display name (denormalised). Rewritten when `renameSpeaker` is called.
        var speaker: String
        /// Stable pyannote label or `"mic"`. Never overwritten.
        var speaker_raw: String

        var text: String
        var words: [Word]
        var overlap: Bool
    }

    struct SpeakerInfo: Codable {
        /// Human-readable display name for the speaker. Mutable via `renameSpeaker`.
        var display_name: String
        /// Audio channel (1 = mic, 2 = system). `nil` for in-person mode.
        var channel: Int?
    }

    struct ModelMeta: Codable {
        let transcription: String
        let diarization: String
        let denoise: String
    }

    // MARK: - Top-level fields

    let job_id: String
    let mode: String          // "remote" | "in_person"
    let created_at: String    // ISO 8601
    let duration_s: Double
    let language: String
    let model: ModelMeta

    var segments: [Segment]
    /// Keyed by `speaker_raw`. This is the single source of truth for current display names.
    var speakers: [String: SpeakerInfo]

    // MARK: - Rename

    /// Renames speaker identified by `rawKey` to `newName`.
    ///
    /// - Validates that `rawKey` exists in `speakers`.
    /// - Validates that `newName` is not already used by another speaker's `display_name`.
    /// - Updates `speakers[rawKey].display_name`.
    /// - Rewrites `speaker` on every segment whose `speaker_raw == rawKey`.
    ///
    /// - Parameters:
    ///   - rawKey: The stable pyannote label (`"SPEAKER_00"`, `"mic"`, etc.).
    ///   - newName: The new display name. Must not collide with any existing display name.
    /// - Throws: `TranscriptError.unknownSpeaker` or `TranscriptError.speakerNameConflict`.
    mutating func renameSpeaker(rawKey: String, to newName: String) throws {
        guard speakers[rawKey] != nil else {
            throw TranscriptError.unknownSpeaker(rawKey)
        }
        // Collision check: newName must not already be a display_name of a different entry.
        if speakers.contains(where: { $0.key != rawKey && $0.value.display_name == newName }) {
            throw TranscriptError.speakerNameConflict(newName)
        }
        speakers[rawKey]?.display_name = newName
        for i in segments.indices where segments[i].speaker_raw == rawKey {
            segments[i].speaker = newName
        }
    }

    // MARK: - Export

    /// Plain-text export: `[Speaker] text` per segment, joined by newlines.
    func toTXT() -> String {
        segments.map { "[\($0.speaker)] \($0.text)" }.joined(separator: "\n")
    }

    /// SRT subtitle export with `Speaker: text` body convention.
    func toSRT() -> String {
        var lines: [String] = []
        for (index, segment) in segments.enumerated() {
            lines.append(String(index + 1))
            lines.append("\(srtTimecode(segment.start)) --> \(srtTimecode(segment.end))")
            lines.append("\(segment.speaker): \(segment.text)")
            lines.append("")  // blank separator
        }
        return lines.joined(separator: "\n")
    }

    /// WebVTT export using `<v Speaker>text</v>` voice tags for native speaker rendering.
    func toVTT() -> String {
        var lines = ["WEBVTT", ""]
        for segment in segments {
            lines.append("\(vttTimecode(segment.start)) --> \(vttTimecode(segment.end))")
            lines.append("<v \(segment.speaker)>\(segment.text)</v>")
            lines.append("")  // blank separator
        }
        return lines.joined(separator: "\n")
    }

    // MARK: - Timecode helpers

    /// HH:MM:SS,mmm format used by SRT.
    private func srtTimecode(_ seconds: Double) -> String {
        let total = Int(seconds * 1000)
        let ms = total % 1000
        let s = (total / 1000) % 60
        let m = (total / 60_000) % 60
        let h = total / 3_600_000
        return String(format: "%02d:%02d:%02d,%03d", h, m, s, ms)
    }

    /// HH:MM:SS.mmm format used by WebVTT.
    private func vttTimecode(_ seconds: Double) -> String {
        let total = Int(seconds * 1000)
        let ms = total % 1000
        let s = (total / 1000) % 60
        let m = (total / 60_000) % 60
        let h = total / 3_600_000
        return String(format: "%02d:%02d:%02d.%03d", h, m, s, ms)
    }
}
