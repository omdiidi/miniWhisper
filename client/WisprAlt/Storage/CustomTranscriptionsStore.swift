import AppKit
import Foundation

/// File-system helpers backing the "Custom Transcriptions" workflow.
///
/// All members are `@MainActor` because `Settings.shared` is main-actor-bound
/// (it's the popover's `@EnvironmentObject`). Callers from `MenuBarController`
/// and SwiftUI views are already on the main actor so this is free.
@MainActor
enum CustomTranscriptionsStore {
    // MARK: - Locations

    /// `<meetingsPath>/Custom Transcriptions` — the parent of every per-job
    /// subfolder produced by the custom-transcription pipeline.
    static var directoryURL: URL {
        Settings.shared.meetingsPath
            .appendingPathComponent("Custom Transcriptions", isDirectory: true)
    }

    // MARK: - Per-job directory

    /// Build (and create on disk) `<directoryURL>/<stem>__<yyyymmdd-HHmmss>`.
    ///
    /// On collision (the timestamp is second-resolution; double-clicks are plausible)
    /// the suffix `-2`, `-3`, … is appended until a fresh path is found, then that
    /// directory is created.
    static func makeJobDirectory(forStem stem: String, now: Date = .now) throws -> URL {
        let timestamp = timestampString(now)
        let baseName = "\(stem)__\(timestamp)"
        let parent = directoryURL

        // Ensure the Custom Transcriptions parent exists.
        try FileManager.default.createDirectory(
            at: parent,
            withIntermediateDirectories: true
        )

        var candidate = parent.appendingPathComponent(baseName, isDirectory: true)
        var suffix = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = parent.appendingPathComponent(
                "\(baseName)-\(suffix)",
                isDirectory: true
            )
            suffix += 1
        }

        try FileManager.default.createDirectory(
            at: candidate,
            withIntermediateDirectories: false
        )
        return candidate
    }

    // MARK: - Newest-transcript lookup

    /// Newest `.txt` directly under `<meetingsPath>` (non-recursive).
    /// Excludes directory entries defensively so a future subdirectory promotion
    /// can't poison the lookup.
    static func newestMeetingTranscript() -> URL? {
        let meetingsRoot = Settings.shared.meetingsPath
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: meetingsRoot,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else {
            return nil
        }
        let txts = entries.filter {
            !$0.hasDirectoryPath && $0.pathExtension.lowercased() == "txt"
        }
        return txts.max { lhs, rhs in
            mtime(lhs) < mtime(rhs)
        }
    }

    /// Newest `.txt` across all per-job subfolders inside `Custom Transcriptions/`.
    static func newestCustomTranscript() -> URL? {
        let root = directoryURL
        guard let subdirs = try? FileManager.default.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return nil
        }
        let dirs = subdirs.filter { $0.hasDirectoryPath }
        let candidates: [URL] = dirs.compactMap { dir in
            guard let entries = try? FileManager.default.contentsOfDirectory(
                at: dir,
                includingPropertiesForKeys: [.contentModificationDateKey],
                options: [.skipsHiddenFiles]
            ) else { return nil }
            let txts = entries.filter {
                !$0.hasDirectoryPath && $0.pathExtension.lowercased() == "txt"
            }
            return txts.max { mtime($0) < mtime($1) }
        }
        return candidates.max { mtime($0) < mtime($1) }
    }

    // MARK: - Pasteboard

    /// Read `url` (UTF-8) and copy its contents to the system pasteboard.
    /// Returns the character count (for the inline "Copied — N chars" toast).
    /// Throws on read failure.
    @discardableResult
    static func copyToPasteboard(_ url: URL) throws -> Int {
        let s = try String(contentsOf: url, encoding: .utf8)
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(s, forType: .string)
        return s.count
    }

    // MARK: - Internals

    /// POSIX-locale formatter so the timestamp is stable across user locales.
    private static let posixTimestampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f
    }()

    private static func timestampString(_ date: Date) -> String {
        posixTimestampFormatter.string(from: date)
    }

    /// Read the modification time via `URLResourceValues.contentModificationDateKey`.
    /// Never use `attributesOfItem(atPath:)[.modificationDate]` — it can return
    /// stale values on APFS clones.
    private static func mtime(_ url: URL) -> Date {
        (try? url.resourceValues(forKeys: [.contentModificationDateKey])
            .contentModificationDate) ?? .distantPast
    }
}
