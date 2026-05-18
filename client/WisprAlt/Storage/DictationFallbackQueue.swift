import Foundation
import Darwin

/// Filesystem-backed queue for cloud-fallback (OpenRouter) dictations that
/// need to be telemetered back to the WisprAlt origin once it's reachable
/// again. Items live at:
///
///     ~/Library/Application Support/co.wispralt/cloud-fallback-queue/<client_dedup_id>.json
///
/// Each file is one JSON object containing the dedup id, transcribed text,
/// UTC dictation timestamp, word count, and the client app version — the
/// shape the server's `POST /telemetry/cloud-dictation` accepts — plus a
/// small set of local-only bookkeeping fields (`created_at`, `retry_count`,
/// `last_attempt_at`) that are stripped before the POST body is built.
///
/// Plan A §A8 — Phase 3 of the personal-history + cloud-fallback rollout.
/// See `docs/FALLBACK.md` for the cloud-fallback path itself and
/// `tmp/ready-plans/2026-05-15-plan-a-me-history-and-cloud-telemetry.md`
/// for design context.
///
/// Drain triggers (any one fires `drain()`):
///   - app foreground (`NSApplication.didBecomeActiveNotification`)
///   - successful online dictation (MenuBarController.dictationStop)
///
/// Concurrent drain calls are coalesced into one in-flight task by
/// `DictationFallbackDrainCoordinator` (mirrors `PendingUploadsQueue`'s
/// drain coordinator). Items that fail 5 times — or sit in the queue more
/// than 7 days — move to a `failed/` sibling directory so they don't retry
/// forever and don't get silently lost.
final class DictationFallbackQueue {
    static let shared = DictationFallbackQueue()

    /// Maximum batch size — must stay ≤ the server's `CloudDictationBatch`
    /// max_length of 200 (see server/src/wispralt_server/routes/telemetry.py).
    private static let batchLimit = 200

    /// Maximum drain attempts before moving an item to `failed/`.
    private static let maxAttempts = 5

    /// Drop entries older than this many seconds (7 days). Beyond this
    /// window the dictation is effectively useless for /me/history /
    /// period insights, so we move it to `failed/` rather than retry
    /// forever.
    private static let maxAgeSec: TimeInterval = 7 * 86_400

    private let dir: URL
    private let failedDir: URL
    private let coordinator = DictationFallbackDrainCoordinator()

    init() {
        let base = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Library/Application Support")
        self.dir = base.appendingPathComponent(
            "co.wispralt/cloud-fallback-queue",
            isDirectory: true
        )
        self.failedDir = dir.appendingPathComponent("failed", isDirectory: true)
    }

    // MARK: - Entry model

    /// One queued dictation. Wire fields (sent to the server) plus local
    /// bookkeeping fields. The encoder writes all fields to disk; the
    /// `wirePayload()` helper drops the bookkeeping ones before POST.
    private struct Entry: Codable {
        // --- wire fields (must match server's CloudDictation pydantic model) ---
        let client_dedup_id: String
        let text: String
        let dictated_at: Double
        let word_count: Int
        let client_app_version: String?

        // --- local-only bookkeeping ---
        let created_at: Double
        var retry_count: Int
        var last_attempt_at: Double?

        /// Build the dict the server expects (no bookkeeping fields).
        func wirePayload() -> [String: Any] {
            var d: [String: Any] = [
                "client_dedup_id": client_dedup_id,
                "text": text,
                "dictated_at": dictated_at,
                "word_count": word_count,
            ]
            if let v = client_app_version {
                d["client_app_version"] = v
            } else {
                d["client_app_version"] = NSNull()
            }
            return d
        }
    }

    // MARK: - Enqueue

    /// Persist a successful cloud-fallback dictation to disk for later sync.
    ///
    /// Non-throwing on purpose: callers (DictationAPI.callOpenRouter) must
    /// never fail the user's primary "got text" flow because of a queue
    /// hiccup. Internal errors are logged at WARNING and swallowed.
    func enqueue(text: String, dictatedAt: Date, clientAppVersion: String?) {
        let id = UUID().uuidString
        let now = Date().timeIntervalSince1970
        let wordCount = Self.countWords(in: text)
        let entry = Entry(
            client_dedup_id: id,
            text: text,
            dictated_at: dictatedAt.timeIntervalSince1970,
            word_count: wordCount,
            client_app_version: clientAppVersion,
            created_at: now,
            retry_count: 0,
            last_attempt_at: nil
        )
        do {
            try ensureDir()
            try writeEntry(entry)
            Log.info(
                "DictationFallbackQueue: enqueued \(id).json (\(wordCount) words)",
                category: "fallback-queue"
            )
        } catch {
            Log.warning(
                "DictationFallbackQueue: enqueue failed for \(id): \(error.localizedDescription)",
                category: "fallback-queue"
            )
        }
    }

    /// Word-count heuristic that matches the server-side computation
    /// (split on whitespace, drop empties). Cheap and good enough for
    /// telemetry / insights aggregates.
    private static func countWords(in text: String) -> Int {
        text.split(whereSeparator: { $0.isWhitespace || $0.isNewline })
            .filter { !$0.isEmpty }
            .count
    }

    // MARK: - Drain

    /// Coalesced drain — concurrent callers all await the same in-flight task.
    /// Safe to call from any thread; never throws to caller.
    func drain() async {
        await coordinator.drain { [weak self] in
            await self?.drainOnce()
        }
    }

    /// One drain pass: read up to `batchLimit` entries, POST as one batch,
    /// delete on 2xx. See file header for the per-status-class behavior.
    private func drainOnce() async {
        // Skip entirely if the user isn't logged in — no token means the
        // server will 401 us anyway. Defer the work to the next foreground
        // trigger.
        guard let apiKey = try? KeychainHelper.getAPIKey(), let key = apiKey, !key.isEmpty else {
            Log.debug(
                "DictationFallbackQueue: no API key configured; skipping drain.",
                category: "fallback-queue"
            )
            return
        }

        let files = listQueueFiles()
        guard !files.isEmpty else { return }

        // First pass: sweep stale (>7d) entries into failed/ so they don't
        // clog the batch. Also load each entry — broken JSON moves to
        // failed/ too (one bad file shouldn't block the rest forever).
        var batch: [(url: URL, entry: Entry)] = []
        batch.reserveCapacity(min(files.count, Self.batchLimit))
        let now = Date().timeIntervalSince1970
        for url in files {
            if batch.count >= Self.batchLimit { break }
            guard let entry = readEntry(at: url) else {
                Log.warning(
                    "DictationFallbackQueue: unreadable entry \(url.lastPathComponent) → failed/",
                    category: "fallback-queue"
                )
                moveToFailed(url)
                continue
            }
            if now - entry.created_at > Self.maxAgeSec {
                Log.warning(
                    "DictationFallbackQueue: \(url.lastPathComponent) older than 7d → failed/",
                    category: "fallback-queue"
                )
                moveToFailed(url)
                continue
            }
            batch.append((url, entry))
        }

        guard !batch.isEmpty else { return }

        Log.info(
            "DictationFallbackQueue: drain starting (batch=\(batch.count))",
            category: "fallback-queue"
        )

        // Build the request via ServerClient so we pick up the configured
        // serverURL, the Bearer token, and the X-WisprAlt-Client-Version
        // header — same pattern every other client call uses.
        let body: Data
        do {
            let payload: [String: Any] = [
                "dictations": batch.map { $0.entry.wirePayload() },
            ]
            body = try JSONSerialization.data(withJSONObject: payload)
        } catch {
            Log.error(
                "DictationFallbackQueue: failed to serialize batch: \(error.localizedDescription)",
                category: "fallback-queue"
            )
            return
        }

        let request: URLRequest
        do {
            request = try ServerClient.shared.buildRequest(
                path: "/telemetry/cloud-dictation",
                method: "POST",
                body: body,
                contentType: "application/json"
            )
        } catch {
            Log.warning(
                "DictationFallbackQueue: cannot build request (\(error.localizedDescription)); aborting drain.",
                category: "fallback-queue"
            )
            return
        }

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            Log.warning(
                "DictationFallbackQueue: transport error on drain (\(error.localizedDescription)); will retry next trigger.",
                category: "fallback-queue"
            )
            return
        }

        guard let http = response as? HTTPURLResponse else {
            Log.warning(
                "DictationFallbackQueue: non-HTTP response on drain; will retry next trigger.",
                category: "fallback-queue"
            )
            return
        }

        let status = http.statusCode

        if (200...299).contains(status) {
            // 2xx — server processed the batch. Trust the count and delete
            // every file we included. Dedup hits are NOT failures: the server
            // returns {inserted, received} and inserted < received simply
            // means an earlier drain already landed those rows.
            var deleted = 0
            for item in batch {
                do {
                    try FileManager.default.removeItem(at: item.url)
                    deleted += 1
                } catch {
                    Log.warning(
                        "DictationFallbackQueue: 2xx but rm failed for \(item.url.lastPathComponent): \(error.localizedDescription)",
                        category: "fallback-queue"
                    )
                }
            }
            let serverReport = String(data: data, encoding: .utf8)?.prefix(200) ?? ""
            Log.info(
                "DictationFallbackQueue: drain finished (drained=\(deleted) failed=0) server=\(serverReport)",
                category: "fallback-queue"
            )
            return
        }

        if status == 401 {
            // Token revoked / unconfigured. Existing UX (dictation error
            // toast) surfaces this elsewhere; no point bumping retry_count
            // for a problem the user has to fix manually.
            Log.warning(
                "DictationFallbackQueue: drain → 401 (API key rejected). Aborting; user must re-paste key in Settings.",
                category: "fallback-queue"
            )
            return
        }

        if (500...599).contains(status) {
            // Server hiccup — leave files in place, retry on next trigger.
            let bodyStr = String(data: data, encoding: .utf8)?.prefix(200) ?? ""
            Log.warning(
                "DictationFallbackQueue: drain → \(status) (server error). Body: \(bodyStr). Will retry next trigger.",
                category: "fallback-queue"
            )
            return
        }

        // 4xx non-401 — bump retry_count on each entry, move to failed/
        // once a single entry hits maxAttempts. We treat 429 as a 4xx
        // here (the batch will be re-tried on the next trigger after
        // retry_count bumps).
        let bodyStr = String(data: data, encoding: .utf8)?.prefix(200) ?? ""
        Log.warning(
            "DictationFallbackQueue: drain → \(status) (client error). Body: \(bodyStr). Bumping retry_count on \(batch.count) entries.",
            category: "fallback-queue"
        )
        var movedToFailed = 0
        let attemptedAt = Date().timeIntervalSince1970
        for item in batch {
            var bumped = item.entry
            bumped.retry_count += 1
            bumped.last_attempt_at = attemptedAt
            if bumped.retry_count >= Self.maxAttempts {
                moveToFailed(item.url)
                movedToFailed += 1
                Log.error(
                    "DictationFallbackQueue: \(item.url.lastPathComponent) exceeded \(Self.maxAttempts) attempts → failed/",
                    category: "fallback-queue"
                )
            } else {
                do {
                    try rewriteEntry(bumped, at: item.url)
                } catch {
                    Log.warning(
                        "DictationFallbackQueue: failed to rewrite \(item.url.lastPathComponent): \(error.localizedDescription)",
                        category: "fallback-queue"
                    )
                }
            }
        }
        Log.info(
            "DictationFallbackQueue: drain finished (drained=0 failed=\(movedToFailed) bumped=\(batch.count - movedToFailed))",
            category: "fallback-queue"
        )
    }

    // MARK: - Filesystem helpers

    private func ensureDir() throws {
        try FileManager.default.createDirectory(
            at: dir,
            withIntermediateDirectories: true
        )
    }

    private func ensureFailedDir() throws {
        try FileManager.default.createDirectory(
            at: failedDir,
            withIntermediateDirectories: true
        )
    }

    /// All `.json` queue files (excludes the `failed/` subdirectory).
    /// Sorted oldest-first so older dictations drain first.
    private func listQueueFiles() -> [URL] {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles, .skipsSubdirectoryDescendants]
        ) else { return [] }
        return entries
            .filter { $0.pathExtension.lowercased() == "json" }
            .sorted { a, b in
                let am = (try? a.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? .distantPast
                let bm = (try? b.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? .distantPast
                return am < bm
            }
    }

    private func readEntry(at url: URL) -> Entry? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(Entry.self, from: data)
    }

    /// Atomic write: `.tmp` → fsync → rename → fsync parent dir. Same
    /// crash-safety pattern as PendingUploadsQueue.
    private func writeEntry(_ entry: Entry) throws {
        let dest = dir.appendingPathComponent("\(entry.client_dedup_id).json")
        try writeEntry(entry, at: dest)
    }

    private func rewriteEntry(_ entry: Entry, at dest: URL) throws {
        try writeEntry(entry, at: dest)
    }

    private func writeEntry(_ entry: Entry, at dest: URL) throws {
        let tmp = dest.appendingPathExtension("tmp")
        try? FileManager.default.removeItem(at: tmp)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(entry)
        try data.write(to: tmp, options: .atomic)
        // fsync the temp file so the bytes are durable before rename.
        if let fh = try? FileHandle(forUpdating: tmp) {
            try? fh.synchronize()
            try? fh.close()
        }
        // Use replaceItem so the swap is atomic even if `dest` already
        // exists (retry_count rewrite path).
        if FileManager.default.fileExists(atPath: dest.path) {
            try? FileManager.default.removeItem(at: dest)
        }
        try FileManager.default.moveItem(at: tmp, to: dest)
        // fsync the parent dir so the rename is durable.
        let fd = open(dir.path, O_RDONLY)
        if fd >= 0 {
            _ = fsync(fd)
            close(fd)
        }
    }

    private func moveToFailed(_ url: URL) {
        do {
            try ensureFailedDir()
        } catch {
            Log.warning(
                "DictationFallbackQueue: cannot create failed/: \(error.localizedDescription)",
                category: "fallback-queue"
            )
            return
        }
        let dest = failedDir.appendingPathComponent(url.lastPathComponent)
        try? FileManager.default.removeItem(at: dest)
        try? FileManager.default.moveItem(at: url, to: dest)
    }
}

/// Coalesces concurrent drain requests so we never run two drain loops at
/// once. Late callers await the in-flight task and return when it finishes.
/// Mirrors `PendingUploadsDrainCoordinator`.
actor DictationFallbackDrainCoordinator {
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
