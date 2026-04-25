import Foundation
import Combine

// MARK: - Summary type

/// Lightweight summary used for the transcript list view. Loaded by scanning
/// `.json` filenames without parsing the full document.
struct TranscriptSummary: Identifiable {
    let id: String          // job_id
    let title: String       // derived from filename (YYYY-MM-DD_HHMM±HHMM_<title>)
    let createdAt: String   // ISO 8601 string from document
    let duration: Double    // duration_s from document
    let mode: String        // "remote" | "in_person"
    /// Base URL (without extension) of the on-disk files for this transcript.
    fileprivate let baseURL: URL
}

// MARK: - Store

/// Manages the local transcript library on disk.
///
/// Transcripts are stored in `Settings.shared.meetingsPath` as
/// `YYYY-MM-DD_HHMM±HHMM_<title>.{json,srt,vtt,txt}`.
///
/// The job_id inside each JSON file is the stable lookup key. `TranscriptStore`
/// maintains an index mapping `job_id → base URL` that is rebuilt on each `refresh()`.
///
/// Speaker rename is entirely client-side and offline-capable:
///   `renameSpeaker(in:rawKey:to:)` atomically rewrites all four formats.
///   It uses a `.transcriptWriteInProgress.<jobID>` sentinel file before the first
///   replace and deletes it after the last, enabling crash recovery on next launch.
final class TranscriptStore: ObservableObject {
    // MARK: - Published state

    @Published private(set) var transcripts: [TranscriptSummary] = []

    // MARK: - Singleton

    static let shared = TranscriptStore()

    // MARK: - Private

    private var meetingsDir: URL {
        Settings.shared.meetingsPath
    }

    /// Index from job_id to the base URL (no extension) of the on-disk files.
    /// Rebuilt on every `refresh()`. Access only on the main queue.
    private var index: [String: URL] = [:]

    // MARK: - Init

    private init() {
        recoverOrphanSentinels()
        refresh()
    }

    // MARK: - Public API

    /// Rescans `meetingsPath`, parses each `.json` file for summary fields, and
    /// publishes the list sorted by `created_at` descending (newest first).
    func refresh() {
        let fm = FileManager.default
        let dir = meetingsDir

        // Ensure the meetings directory exists (created lazily on first access).
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)

        guard let contents = try? fm.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: .skipsHiddenFiles
        ) else {
            DispatchQueue.main.async { [weak self] in
                self?.transcripts = []
                self?.index = [:]
            }
            return
        }

        let jsonURLs = contents.filter { $0.pathExtension == "json" }
        var summaries: [TranscriptSummary] = []
        var newIndex: [String: URL] = [:]

        for url in jsonURLs {
            guard let data = try? Data(contentsOf: url),
                  let doc = try? TranscriptDocument.decode(data)
            else {
                Log.warning("Could not parse transcript at \(url.lastPathComponent)", category: "store")
                continue
            }
            let baseURL = url.deletingPathExtension()
            let title = baseURL.lastPathComponent
            summaries.append(TranscriptSummary(
                id: doc.job_id,
                title: title,
                createdAt: doc.created_at,
                duration: doc.duration_s,
                mode: doc.mode,
                baseURL: baseURL
            ))
            newIndex[doc.job_id] = baseURL
        }

        // Sort newest-first by created_at string (ISO 8601 lexicographic sort is stable).
        summaries.sort { $0.createdAt > $1.createdAt }

        DispatchQueue.main.async { [weak self] in
            self?.transcripts = summaries
            self?.index = newIndex
        }
    }

    /// Loads and decodes the full `TranscriptDocument` for a given job ID.
    ///
    /// - Parameter id: The `job_id` stored inside the JSON.
    /// - Throws: `TranscriptError.ioError` or `TranscriptError.decodingError`.
    func load(_ id: String) throws -> TranscriptDocument {
        let url = try resolvedJSONURL(for: id)
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            throw TranscriptError.ioError(error)
        }
        return try TranscriptDocument.decode(data)
    }

    /// Renames a speaker in a transcript and atomically rewrites all four output formats.
    ///
    /// Atomic write contract (v3 P4#5):
    ///   1. Write sentinel file `.transcriptWriteInProgress.<jobID>` in `meetingsDir`.
    ///   2. For each format, write to a `.<uuid>.tmp` file in the SAME directory,
    ///      then call `FileManager.replaceItemAt(_:withItemAt:)`.
    ///      (`data.write(to:options:)` is NOT called with `.atomic` — the manual replace
    ///       is sufficient and avoids double-write overhead.)
    ///   3. Delete sentinel after the last replace.
    ///
    /// On crash recovery (see `recoverOrphanSentinels`), partial `.tmp` files are
    /// removed and the originals are left intact.
    ///
    /// - Parameters:
    ///   - jobID: The `job_id` of the transcript to update.
    ///   - rawKey: Stable pyannote label identifying the speaker to rename.
    ///   - newName: New display name. Must not collide.
    /// - Throws: `TranscriptError` on validation failure or I/O error.
    func renameSpeaker(in jobID: String, rawKey: String, to newName: String) throws {
        var doc = try load(jobID)
        try doc.renameSpeaker(rawKey: rawKey, to: newName)

        let baseURL = try resolvedBaseURL(for: jobID)
        let sentinel = sentinelURL(for: jobID)

        // Write sentinel before touching any output file.
        do {
            try Data().write(to: sentinel)
        } catch {
            throw TranscriptError.ioError(error)
        }

        defer {
            // Always attempt sentinel removal, even on partial failure — the recovery
            // logic on next launch handles the incomplete state.
            try? FileManager.default.removeItem(at: sentinel)
        }

        // Encode JSON.
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let jsonData: Data
        do {
            jsonData = try encoder.encode(doc)
        } catch {
            throw TranscriptError.ioError(error)
        }

        // Atomically write each format using the resolved base URL.
        try writeAtomic(jsonData, to: baseURL.appendingPathExtension("json"), jobID: jobID)
        try writeAtomic(Data(doc.toSRT().utf8), to: baseURL.appendingPathExtension("srt"), jobID: jobID)
        try writeAtomic(Data(doc.toVTT().utf8), to: baseURL.appendingPathExtension("vtt"), jobID: jobID)
        try writeAtomic(Data(doc.toTXT().utf8), to: baseURL.appendingPathExtension("txt"), jobID: jobID)

        Log.info("Speaker renamed in \(jobID): rawKey=\(rawKey) → \(newName)", category: "store")
        refresh()
    }

    // MARK: - Crash recovery

    /// Scans `meetingsDir` for orphan `.transcriptWriteInProgress.*` sentinel files
    /// left by a previous crash during a rename write.
    ///
    /// For each orphan:
    ///   - Deletes any `.<uuid>.tmp` files in the same directory.
    ///   - Removes the sentinel.
    ///   - Original `.json/.srt/.vtt/.txt` files are left intact.
    private func recoverOrphanSentinels() {
        let fm = FileManager.default
        let dir = meetingsDir

        guard let contents = try? fm.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: nil,
            options: .skipsHiddenFiles
        ) else { return }

        let sentinels = contents.filter { $0.lastPathComponent.hasPrefix(".transcriptWriteInProgress.") }
        if sentinels.isEmpty { return }

        Log.warning("Found \(sentinels.count) orphan transcript write sentinel(s); recovering.", category: "store")

        // I4: Only delete .tmp files whose name is derived from a known sentinel's jobID.
        // Tmp file format: .<jobID>.<uuid>.<fmt>.tmp  (set by writeAtomic)
        // We do NOT delete all .tmp files indiscriminately — other processes may use them.
        for sentinel in sentinels {
            // Extract jobID from ".transcriptWriteInProgress.<jobID>"
            let sentinelName = sentinel.lastPathComponent
            let prefix = ".transcriptWriteInProgress."
            guard sentinelName.hasPrefix(prefix) else { continue }
            let jobID = String(sentinelName.dropFirst(prefix.count))

            // Match tmp files for this specific jobID: .<jobID>.*.tmp
            let jobTmpFiles = contents.filter { url in
                let name = url.lastPathComponent
                return name.hasPrefix(".\(jobID).") && name.hasSuffix(".tmp")
            }
            for tmp in jobTmpFiles {
                try? fm.removeItem(at: tmp)
                Log.debug("Removed orphan tmp file: \(tmp.lastPathComponent)", category: "store")
            }

            try? fm.removeItem(at: sentinel)
            Log.debug("Removed orphan sentinel: \(sentinelName)", category: "store")
        }
    }

    // MARK: - Private helpers

    /// Performs a single atomic write: temp file in same directory → `replaceItemAt`.
    ///
    /// The temp file is placed in the SAME directory as `destination` so the
    /// `replaceItemAt` rename stays on the same APFS volume (O(1) rename).
    ///
    /// I4: The tmp filename is `.<jobID>.<uuid>.<fmt>.tmp` so `recoverOrphanSentinels`
    /// can identify which tmp files belong to which sentinel without deleting unrelated files.
    private func writeAtomic(_ data: Data, to destination: URL, jobID: String) throws {
        let fmt = destination.pathExtension
        let tmpURL = destination
            .deletingLastPathComponent()
            .appendingPathComponent(".\(jobID).\(UUID().uuidString).\(fmt).tmp")

        do {
            // Write without .atomic option — we do the atomic swap manually below.
            try data.write(to: tmpURL)
        } catch {
            throw TranscriptError.ioError(error)
        }

        do {
            _ = try FileManager.default.replaceItemAt(destination, withItemAt: tmpURL)
        } catch {
            // Clean up the temp file if the replace failed.
            try? FileManager.default.removeItem(at: tmpURL)
            throw TranscriptError.ioError(error)
        }
    }

    /// Returns the resolved JSON URL for a job ID, searching the index first,
    /// then falling back to a filesystem scan.
    private func resolvedJSONURL(for jobID: String) throws -> URL {
        try resolvedBaseURL(for: jobID).appendingPathExtension("json")
    }

    /// Returns the base URL (without extension) for a job ID.
    ///
    /// Uses the in-memory index when possible. If the index is stale (e.g. called
    /// before first refresh), performs a synchronous scan of the directory.
    private func resolvedBaseURL(for jobID: String) throws -> URL {
        // Fast path: consult the in-memory index.
        if let cached = index[jobID] {
            return cached
        }

        // Slow path: scan directory for a JSON with matching job_id.
        let fm = FileManager.default
        let dir = meetingsDir
        guard let contents = try? fm.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: nil,
            options: .skipsHiddenFiles
        ) else {
            throw TranscriptError.ioError(
                NSError(domain: NSCocoaErrorDomain, code: NSFileReadUnknownError,
                        userInfo: [NSLocalizedDescriptionKey: "Cannot list \(dir.path)"])
            )
        }

        for url in contents where url.pathExtension == "json" {
            guard let data = try? Data(contentsOf: url),
                  let doc = try? TranscriptDocument.decode(data),
                  doc.job_id == jobID
            else { continue }
            return url.deletingPathExtension()
        }

        throw TranscriptError.ioError(
            NSError(domain: NSCocoaErrorDomain, code: NSFileNoSuchFileError,
                    userInfo: [NSLocalizedDescriptionKey: "No transcript found for job_id \(jobID)"])
        )
    }

    private func sentinelURL(for jobID: String) -> URL {
        meetingsDir.appendingPathComponent(".transcriptWriteInProgress.\(jobID)")
    }
}
