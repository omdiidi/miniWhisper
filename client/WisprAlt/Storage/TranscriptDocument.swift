import Foundation

// MARK: - Errors

/// Typed errors for transcript document operations.
enum TranscriptError: Error, LocalizedError {
    /// The `rawKey` is not present in `speakers`.
    case unknownSpeaker(String)
    /// The requested `newName` is already used as a display name by another speaker.
    case speakerNameConflict(String)
    /// The new name is empty after trimming, or clashes with an existing raw key.
    case invalidSpeakerName(String)
    /// A file system I/O error occurred.
    case ioError(Error)
    /// The JSON file could not be decoded.
    case decodingError(Error)
    /// The decoded JSON is structurally valid but contains out-of-range segment values.
    case malformedJSON(String)

    var errorDescription: String? {
        switch self {
        case .unknownSpeaker(let key):
            return "No speaker found with raw key \"\(key)\"."
        case .speakerNameConflict(let name):
            return "The name \"\(name)\" is already used by another speaker."
        case .invalidSpeakerName(let reason):
            return "Invalid speaker name: \(reason)."
        case .ioError(let underlying):
            return "File I/O error: \(underlying.localizedDescription)"
        case .decodingError(let underlying):
            return "Could not decode transcript: \(underlying.localizedDescription)"
        case .malformedJSON(let detail):
            return "Transcript JSON is malformed: \(detail)"
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
    /// Optional server-emitted warnings (e.g. "mono input — dual-channel mode unavailable").
    var warnings: [String]?

    // MARK: - Decode with validation

    /// Decodes *data* as a `TranscriptDocument` and validates each segment.
    ///
    /// Validation rules per segment:
    /// - `start >= 0`
    /// - `end >= start`
    /// - Both `start` and `end` are finite (not NaN / Inf).
    /// - `text` is never nil (guaranteed by the Codable field type, checked for safety).
    ///
    /// - Throws: `TranscriptError.decodingError` on JSON parse failure.
    ///           `TranscriptError.malformedJSON` on semantic validation failure.
    static func decode(_ data: Data) throws -> TranscriptDocument {
        let doc: TranscriptDocument
        do {
            doc = try JSONDecoder().decode(TranscriptDocument.self, from: data)
        } catch {
            throw TranscriptError.decodingError(error)
        }
        for (i, seg) in doc.segments.enumerated() {
            guard seg.start.isFinite else {
                throw TranscriptError.malformedJSON("segment[\(i)].start is not finite: \(seg.start)")
            }
            guard seg.end.isFinite else {
                throw TranscriptError.malformedJSON("segment[\(i)].end is not finite: \(seg.end)")
            }
            guard seg.start >= 0 else {
                throw TranscriptError.malformedJSON("segment[\(i)].start is negative: \(seg.start)")
            }
            guard seg.end >= seg.start else {
                throw TranscriptError.malformedJSON("segment[\(i)].end (\(seg.end)) < start (\(seg.start))")
            }

            // Per-word timestamp validation.
            var prevWordStart: Double? = nil
            for (j, word) in seg.words.enumerated() {
                if let ws = word.start {
                    guard ws.isFinite else {
                        throw TranscriptError.malformedJSON("segment[\(i)].words[\(j)].start is not finite: \(ws)")
                    }
                    guard ws >= 0 else {
                        throw TranscriptError.malformedJSON("segment[\(i)].words[\(j)].start is negative: \(ws)")
                    }
                    if let prev = prevWordStart, ws < prev {
                        throw TranscriptError.malformedJSON(
                            "segment[\(i)].words[\(j)].start (\(ws)) is not monotonic (prev: \(prev))")
                    }
                    prevWordStart = ws
                }
                if let we = word.end {
                    guard we.isFinite else {
                        throw TranscriptError.malformedJSON("segment[\(i)].words[\(j)].end is not finite: \(we)")
                    }
                    guard we >= 0 else {
                        throw TranscriptError.malformedJSON("segment[\(i)].words[\(j)].end is negative: \(we)")
                    }
                    if let ws = word.start {
                        guard we >= ws else {
                            throw TranscriptError.malformedJSON(
                                "segment[\(i)].words[\(j)].end (\(we)) < start (\(ws))")
                        }
                    }
                }
            }

            // Segment bounds must contain all word timestamps.
            if let firstWord = seg.words.first, let firstStart = firstWord.start {
                guard seg.start <= firstStart else {
                    throw TranscriptError.malformedJSON(
                        "segment[\(i)].start (\(seg.start)) > words[0].start (\(firstStart))")
                }
            }
            if let lastWord = seg.words.last, let lastEnd = lastWord.end {
                guard seg.end >= lastEnd else {
                    throw TranscriptError.malformedJSON(
                        "segment[\(i)].end (\(seg.end)) < words.last.end (\(lastEnd))")
                }
            }
        }
        return doc
    }

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
    /// Returns true if *s* contains control characters, Unicode format characters
    /// (Cf), zero-width spaces/joiners, or RTL/LTR override characters.
    /// These are disallowed in speaker names to prevent display spoofing.
    private static func containsForbiddenChars(_ s: String) -> Bool {
        s.unicodeScalars.contains {
            CharacterSet.controlCharacters.contains($0)
                || ($0.value >= 0x200B && $0.value <= 0x200F)  // zero-width chars
                || ($0.value >= 0x202A && $0.value <= 0x202E)  // LTR/RTL overrides
        }
    }

    mutating func renameSpeaker(rawKey: String, to newName: String) throws {
        // G2: Sanitise the input name before any other check.
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw TranscriptError.invalidSpeakerName("name cannot be empty")
        }
        // Reject names containing control/format/RTL-override characters.
        if TranscriptDocument.containsForbiddenChars(trimmed) {
            throw TranscriptError.invalidSpeakerName("name contains disallowed characters")
        }
        // Reject names that match an existing speaker_raw key (not just display_name).
        if speakers.keys.contains(trimmed) && trimmed != rawKey {
            throw TranscriptError.invalidSpeakerName("name conflicts with an existing raw speaker key")
        }

        guard speakers[rawKey] != nil else {
            throw TranscriptError.unknownSpeaker(rawKey)
        }
        // Collision check: trimmed must not already be a display_name of a different entry.
        if speakers.contains(where: { $0.key != rawKey && $0.value.display_name == trimmed }) {
            throw TranscriptError.speakerNameConflict(trimmed)
        }
        speakers[rawKey]?.display_name = trimmed
        for i in segments.indices where segments[i].speaker_raw == rawKey {
            segments[i].speaker = trimmed
        }
    }

    // MARK: - Escape helpers for subtitle formats

    /// Escapes *s* for inclusion in an SRT cue body.
    /// Replaces newlines with spaces to avoid breaking the SRT block structure.
    static func escapedForSRT(_ s: String) -> String {
        s.replacingOccurrences(of: "\r\n", with: " ")
         .replacingOccurrences(of: "\r",   with: " ")
         .replacingOccurrences(of: "\n",   with: " ")
    }

    /// Escapes *s* for inclusion in a WebVTT cue payload.
    /// HTML-escapes `<`, `>`, `&` and replaces newlines with `<br/>`.
    static func escapedForVTT(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
         .replacingOccurrences(of: "<", with: "&lt;")
         .replacingOccurrences(of: ">", with: "&gt;")
         .replacingOccurrences(of: "\r\n", with: "<br/>")
         .replacingOccurrences(of: "\r",   with: "<br/>")
         .replacingOccurrences(of: "\n",   with: "<br/>")
    }

    // MARK: - Export

    /// Plain-text export: `[Speaker] text` per segment, joined by newlines.
    /// Speaker and text are stripped of control characters to avoid breaking line structure.
    func toTXT() -> String {
        segments.map { seg in
            let speaker = TranscriptDocument.escapedForSRT(seg.speaker)
            let text = TranscriptDocument.escapedForSRT(seg.text)
            return "[\(speaker)] \(text)"
        }.joined(separator: "\n")
    }

    /// SRT subtitle export with `Speaker: text` body convention.
    func toSRT() -> String {
        var lines: [String] = []
        for (index, segment) in segments.enumerated() {
            lines.append(String(index + 1))
            lines.append("\(srtTimecode(segment.start)) --> \(srtTimecode(segment.end))")
            lines.append("\(TranscriptDocument.escapedForSRT(segment.speaker)): \(TranscriptDocument.escapedForSRT(segment.text))")
            lines.append("")  // blank separator
        }
        return lines.joined(separator: "\n")
    }

    /// WebVTT export using `<v Speaker>text</v>` voice tags for native speaker rendering.
    func toVTT() -> String {
        var lines = ["WEBVTT", ""]
        for segment in segments {
            lines.append("\(vttTimecode(segment.start)) --> \(vttTimecode(segment.end))")
            lines.append("<v \(TranscriptDocument.escapedForVTT(segment.speaker))>\(TranscriptDocument.escapedForVTT(segment.text))</v>")
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
