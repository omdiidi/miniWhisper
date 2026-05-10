import Foundation
import Darwin

/// Filesystem-backed queue for meeting recordings whose upload failed because
/// the Mac mini origin was offline. Items live at:
///
///     ~/Library/Application Support/co.wispralt/pending-uploads/<uuid>.<ext>
///
/// where `<ext>` is the source recording's extension — `.m4a` for new
/// post-Task-9 meetings (uploaded via `MeetingAPI.submitFile` →
/// `/transcribe/file`) and `.wav` for legacy entries queued by an older client
/// before the Task-9 cutover (replayed via `MeetingAPI.submit` →
/// `/transcribe/meeting` until `/transcribe/meeting`'s POST is removed in
/// Task 12).
///
/// On `enqueue`, the source is copied atomically (write `.tmp`, fsync file,
/// rename, fsync parent directory) so a crash mid-enqueue can never produce a
/// torn file.
///
/// Drain triggers (any one fires `drain()`):
///   - successful dictation (MenuBarController.dictationStop)
///   - app foreground (`NSApplication.didBecomeActiveNotification`)
///   - 120-second periodic timer
///   - manual menubar action "Retry pending uploads"
///
/// Concurrent drain calls are coalesced into one in-flight task by
/// `PendingUploadsDrainCoordinator`. Items that fail 5 times move to a
/// `failed/` sibling directory so they don't retry forever.
final class PendingUploadsQueue {
    static let shared = PendingUploadsQueue()

    /// Maximum drain attempts before moving an item to `failed/`.
    private static let maxAttempts = 5
    /// Disk-pressure floor — refuse to enqueue when free space drops below this.
    private static let minFreeBytes: Int64 = 2_000_000_000

    private let dir: URL
    private let failedDir: URL
    private let attemptsURL: URL
    private let coordinator = PendingUploadsDrainCoordinator()

    init() {
        let base = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Library/Application Support")
        self.dir = base.appendingPathComponent("co.wispralt/pending-uploads", isDirectory: true)
        self.failedDir = dir.appendingPathComponent("failed", isDirectory: true)
        self.attemptsURL = dir.appendingPathComponent("attempts.json")
    }

    // MARK: - Enqueue

    enum QueueError: Error {
        case diskFull
        case copyFailed(Error)
    }

    /// Recording source extensions we know how to replay. Enqueue refuses
    /// anything else so `drainOnce` never has to ignore a stranded file.
    /// `m4a` is the post-Task-9 format; `wav` is the legacy format kept for
    /// the dual-replay migration window.
    private static let supportedExtensions: Set<String> = ["m4a", "wav"]

    /// Atomically copies `source` into the pending-uploads directory.
    /// The recording pipeline must have already fsync'd the source file
    /// (see `MeetingRecorder.stop()`) — copying does not propagate sync.
    ///
    /// The source's extension is preserved so `drainOnce` can route the
    /// replay to the correct upload endpoint (`.m4a` → `/transcribe/file`,
    /// `.wav` → `/transcribe/meeting`).
    func enqueue(wav source: URL) throws {
        let ext = source.pathExtension.lowercased()
        guard Self.supportedExtensions.contains(ext) else {
            throw QueueError.copyFailed(
                NSError(
                    domain: "PendingUploadsQueue",
                    code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "Unsupported source extension '\(ext)'"]
                )
            )
        }

        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let attrs = try? FileManager.default.attributesOfFileSystem(forPath: dir.path),
           let free = attrs[.systemFreeSize] as? Int64,
           free < Self.minFreeBytes
        {
            throw QueueError.diskFull
        }

        let id = UUID().uuidString
        let dest = dir.appendingPathComponent("\(id).\(ext)")
        let tmp = dir.appendingPathComponent("\(id).\(ext).tmp")

        do {
            try? FileManager.default.removeItem(at: tmp)
            try FileManager.default.copyItem(at: source, to: tmp)
            // fsync the copy so the bytes are durable before rename.
            let fh = try FileHandle(forUpdating: tmp)
            try fh.synchronize()
            try fh.close()
            try FileManager.default.moveItem(at: tmp, to: dest)
            // fsync the parent directory so the rename itself is durable.
            let fd = open(dir.path, O_RDONLY)
            if fd >= 0 {
                _ = fsync(fd)
                close(fd)
            }
            Log.info("PendingUploadsQueue: enqueued \(dest.lastPathComponent)", category: "fallback")
        } catch {
            try? FileManager.default.removeItem(at: tmp)
            throw QueueError.copyFailed(error)
        }
    }

    /// Number of queued items currently awaiting upload (excludes `failed/`).
    /// Counts both `.m4a` (current) and `.wav` (legacy pre-Task-9) entries.
    func count() -> Int {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else { return 0 }
        return entries.filter { Self.supportedExtensions.contains($0.pathExtension.lowercased()) }.count
    }

    /// Sorted oldest-first so the user-visible drain order matches recording order.
    /// Returns both `.m4a` and `.wav` entries; `drainOnce` routes by extension.
    func pending() -> [URL] {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return [] }
        return entries
            .filter { Self.supportedExtensions.contains($0.pathExtension.lowercased()) }
            .sorted { a, b in
                let am = (try? a.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? .distantPast
                let bm = (try? b.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? .distantPast
                return am < bm
            }
    }

    // MARK: - Drain

    /// Coalesced drain — concurrent callers all await the same in-flight task.
    func drain() async {
        await coordinator.drain { [weak self] in
            await self?.drainOnce()
        }
    }

    /// Process all currently-pending items. Safe to call when queue is empty.
    /// Errors on a single item don't stop the loop — the next item gets a chance.
    private func drainOnce() async {
        let items = pending()
        guard !items.isEmpty else { return }
        var attempts = readAttemptCounts()

        for item in items {
            let id = item.deletingPathExtension().lastPathComponent
            if (attempts[id] ?? 0) >= Self.maxAttempts {
                moveToFailed(wav: item)
                attempts.removeValue(forKey: id)
                continue
            }
            do {
                // Route by extension: legacy `.wav` entries (queued by a
                // pre-Task-9 client before the m4a cutover) replay through
                // the legacy `MeetingAPI.submit` → `/transcribe/meeting`
                // path so existing offline meetings are not silently
                // dropped. New `.m4a` entries replay through
                // `MeetingAPI.submitFile` → `/transcribe/file`. Both paths
                // are kept until Task 12 retires the legacy endpoint.
                let ext = item.pathExtension.lowercased()
                switch ext {
                case "wav":
                    _ = try await MeetingAPI.submit(item, progress: { _ in })
                case "m4a":
                    _ = try await MeetingAPI.submitFile(item, progress: { _ in })
                default:
                    // `pending()` filters by `supportedExtensions`, so
                    // anything else here is impossible in practice. Fail
                    // loudly rather than silently drop the file.
                    Log.warning(
                        "PendingUploadsQueue: \(item.lastPathComponent) has unsupported extension '\(ext)'; leaving in place",
                        category: "fallback"
                    )
                    continue
                }
                try? FileManager.default.removeItem(at: item)
                attempts.removeValue(forKey: id)
                Log.info("PendingUploadsQueue: drained \(item.lastPathComponent)", category: "fallback")
            } catch {
                attempts[id, default: 0] += 1
                Log.info(
                    "PendingUploadsQueue: \(item.lastPathComponent) failed (attempt \(attempts[id]!)); will retry",
                    category: "fallback"
                )
                // Continue rather than break — a different item may still succeed.
            }
        }
        writeAttemptCounts(attempts)
    }

    private func moveToFailed(wav: URL) {
        try? FileManager.default.createDirectory(at: failedDir, withIntermediateDirectories: true)
        let dest = failedDir.appendingPathComponent(wav.lastPathComponent)
        try? FileManager.default.moveItem(at: wav, to: dest)
        Log.warning(
            "PendingUploadsQueue: \(wav.lastPathComponent) exceeded \(Self.maxAttempts) attempts → failed/",
            category: "fallback"
        )
    }

    // MARK: - Attempts persistence

    private func readAttemptCounts() -> [String: Int] {
        guard let data = try? Data(contentsOf: attemptsURL),
              let dict = try? JSONDecoder().decode([String: Int].self, from: data)
        else { return [:] }
        return dict
    }

    private func writeAttemptCounts(_ counts: [String: Int]) {
        guard let data = try? JSONEncoder().encode(counts) else { return }
        try? data.write(to: attemptsURL, options: .atomic)
    }
}

/// Coalesces concurrent drain requests so we never run two drain loops at once.
/// Late callers await the in-flight task and return when it finishes.
actor PendingUploadsDrainCoordinator {
    private var inFlight: Task<Void, Never>?

    func drain(_ work: @Sendable @escaping () async -> Void) async {
        if let existing = inFlight {
            await existing.value
            return
        }
        let task = Task { await work() }
        inFlight = task
        await task.value
        inFlight = nil
    }
}
