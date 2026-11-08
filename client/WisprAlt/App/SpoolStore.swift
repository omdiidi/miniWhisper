import AppKit
import AVFoundation
import Foundation
import WisprAltCore

/// Paths under `~/Library/Application Support/WisprAlt/` (0700) and the two
/// synchronous FN-press helpers. No state. The FN-press side owns an id until
/// `recorder.start` returns; after FN-up only `SpoolStore` touches it.
enum SpoolFiles {
    static let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("WisprAlt", isDirectory: true)
    static let spoolDir = base.appendingPathComponent("spool", isDirectory: true)
    static let retryDir = base.appendingPathComponent("retry", isDirectory: true)
    static let lastTextURL = base.appendingPathComponent("last.json")

    /// The in-flight recording (CAF tolerates an unfinished data chunk, so a
    /// crash mid-dictation still leaves a readable file).
    static func audioURL(_ id: String) -> URL { spoolDir.appendingPathComponent("\(id).caf") }
    static func sidecarURL(_ id: String, in dir: URL = spoolDir) -> URL { dir.appendingPathComponent("\(id).json") }

    /// One ~40-byte atomic 0600 write on the main thread, finished before
    /// `recorder.start` (so before the spool file exists).
    nonisolated static func writeSidecarSync(id: String, _ sidecar: SpoolSidecar) throws {
        try PrivateFile.write(try sidecar.encoded(), to: sidecarURL(id))
    }

    nonisolated static func removeSidecarSync(_ id: String) {
        try? FileManager.default.removeItem(at: sidecarURL(id))
    }

    /// Ids present in `spool/` right now (audio or sidecar). Taken once at
    /// launch, before any dictation can start.
    static func spoolIDs() -> [String] { ids(in: spoolDir) }

    static func ids(in dir: URL) -> [String] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return Array(Set(names.filter { !$0.hasPrefix(".") }.map { ($0 as NSString).deletingPathExtension }))
    }
}

/// The last-dictation record (`last.json`), read by "Copy last dictation".
enum LastTextStore {
    static func save(_ result: DictationResult, mode: TranscriptionMode) {
        do {
            try LastText(text: result.text, at: Date(), mode: mode, modelUsed: result.modelUsed)
                .save(to: SpoolFiles.lastTextURL)
        } catch {
            Log.warning("last-text save failed: \(ErrorLog.describe(error))", category: "dictation")
        }
    }

    static func load() -> LastText? { LastText.load(from: SpoolFiles.lastTextURL) }
}

/// A saved recording in `retry/`, as the popover lists it.
struct RetryItem: Identifiable, Equatable {
    let id: String
    let createdAt: Date
    let durationSeconds: Double
    let audioURL: URL
    let sidecar: Data?
}

enum RetryError: LogSafeError {
    case notRetryable
    var logDescription: String { "retry(notRetryable)" }
    var outcomeKind: ErrorOutcome { .failed(.other) }
}

/// Every spool / retry file operation after FN-up runs here, serially:
/// the dictation disposition, launch recovery, Retry / Export / Discard and
/// the retry-list cap.
actor SpoolStore {
    static let shared = SpoolStore()
    private let fm = FileManager.default

    /// Deletes a dictation's spooled audio and sidecar.
    func delete(_ id: String) {
        try? fm.removeItem(at: SpoolFiles.audioURL(id))
        try? fm.removeItem(at: SpoolFiles.sidecarURL(id))
    }

    /// Moves a spooled dictation to `retry/` as a 16 kHz mono WAV (the raw
    /// `.caf` if conversion fails), then applies the newest-2 / 7-day cap.
    /// - Returns: whether audio was kept, and how many older entries the cap dropped.
    @discardableResult
    func keep(_ id: String) -> (kept: Bool, dropped: Int) {
        let kept = moveToRetry(id)
        return (kept, prune())
    }

    /// Launch: every spooled id from the last session with audio > 0 s goes to
    /// `retry/`; the rest (and orphan sidecars) are deleted. Nothing is sent.
    /// - Returns: how many recordings were saved.
    func recover(_ ids: [String]) -> Int {
        for id in ids {
            if Self.duration(SpoolFiles.audioURL(id)) > 0 { _ = moveToRetry(id) } else { delete(id) }
        }
        prune()
        return ids.filter { retryAudio($0) != nil }.count
    }

    /// `retry/` entries, newest first (after pruning).
    func list() -> [RetryItem] {
        prune()
        return SpoolFiles.ids(in: SpoolFiles.retryDir).compactMap { id -> RetryItem? in
            guard let audio = retryAudio(id) else { return nil }
            return RetryItem(id: id, createdAt: Self.created(audio), durationSeconds: Self.duration(audio),
                             audioURL: audio, sidecar: try? Data(contentsOf: SpoolFiles.sidecarURL(id, in: SpoolFiles.retryDir)))
        }.sorted { $0.createdAt > $1.createdAt }
    }

    func discard(_ id: String) {
        for ext in ["wav", "caf", "json"] {
            try? fm.removeItem(at: SpoolFiles.retryDir.appendingPathComponent("\(id).\(ext)"))
        }
    }

    func export(_ id: String, to dest: URL) throws {
        guard let audio = retryAudio(id) else { throw CocoaError(.fileNoSuchFile) }
        if fm.fileExists(atPath: dest.path) { try fm.removeItem(at: dest) }
        try fm.copyItem(at: audio, to: dest)
    }

    /// Re-sends a saved recording on the route its sidecar names (only when
    /// that is the current mode). Online → `OpenAIFileProvider` with the
    /// sidecar's file model; Local → whole file to the current host with
    /// `X-Client-Dedup-Id: <id>`. The entry is deleted on success (kept on a
    /// Local `saved: false`) and on a terminal 410/409; any other error keeps it.
    func retry(_ id: String, mode: TranscriptionMode) async throws -> DictationResult {
        let sidecarData = try? Data(contentsOf: SpoolFiles.sidecarURL(id, in: SpoolFiles.retryDir))
        guard RetryEntries.actions(sidecar: sidecarData, currentMode: mode).actions.contains(.retry),
              let sidecar = SpoolSidecar.decode(sidecarData), let audio = retryAudio(id)
        else { throw RetryError.notRetryable }
        let recording = Recording(fileURL: audio, durationSeconds: Self.duration(audio), interruptedByDeviceChange: false)
        do {
            let result: DictationResult
            switch sidecar.mode {
            case .online:
                result = try await OpenAIFileProvider(model: OpenAIModels.fileModel(for: sidecar.model),
                                                      keyProvider: { OpenAIKey.current() }).finish(recording)
            case .local:
                let wav = try Mono16kWAV.make(from: audio)
                result = try await LocalServerProvider(endpoint: ServerClient.shared.currentEndpoint(), dictationId: id)
                    .retry(wav: wav, durationSeconds: recording.durationSeconds)
            }
            if result.serverSaved { discard(id) }
            return result
        } catch {
            if ErrorLog.outcome(error) == .failed(.rejected) { discard(id) }
            throw error
        }
    }

    // MARK: - Private

    private func moveToRetry(_ id: String) -> Bool {
        let src = SpoolFiles.audioURL(id)
        var kept = false
        if fm.fileExists(atPath: src.path) {
            do {
                try PrivateFile.write(try Mono16kWAV.make(from: src),
                                      to: SpoolFiles.retryDir.appendingPathComponent("\(id).wav"))
                kept = true
            } catch {
                Log.warning("retry: 16 kHz conversion failed (\(ErrorLog.describe(error))); keeping the raw file.",
                            category: "dictation")
                kept = (try? fm.moveItem(at: src, to: SpoolFiles.retryDir.appendingPathComponent("\(id).caf"))) != nil
            }
        }
        let sidecarDest = SpoolFiles.sidecarURL(id, in: SpoolFiles.retryDir)
        try? fm.removeItem(at: sidecarDest)
        try? fm.moveItem(at: SpoolFiles.sidecarURL(id), to: sidecarDest)
        delete(id)
        return kept
    }

    /// Applies `RetryEntries.plan` to `retry/`. Returns the entries dropped for space.
    @discardableResult
    private func prune() -> Int {
        let entries = SpoolFiles.ids(in: SpoolFiles.retryDir).map { id -> RetryEntries.Entry in
            let audio = retryAudio(id)
            return RetryEntries.Entry(id: id, createdAt: audio.map(Self.created) ?? .distantPast,
                                      durationSeconds: audio.map(Self.duration) ?? 0, hasAudio: audio != nil)
        }
        let plan = RetryEntries.plan(entries, now: Date())
        plan.delete.forEach(discard)
        return plan.droppedForSpace
    }

    private func retryAudio(_ id: String) -> URL? {
        ["wav", "caf"].map { SpoolFiles.retryDir.appendingPathComponent("\(id).\($0)") }
            .first { fm.fileExists(atPath: $0.path) }
    }

    private static func duration(_ url: URL) -> Double {
        guard let file = try? AVAudioFile(forReading: url), file.processingFormat.sampleRate > 0 else { return 0 }
        return Double(file.length) / file.processingFormat.sampleRate
    }

    private static func created(_ url: URL) -> Date {
        (try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
    }
}

// MARK: - MenuBarController glue (main actor)

extension MenuBarController {
    /// Applies a dictation's disposition on the store, then shows its toasts.
    @MainActor func applySpool(_ decision: SpoolDecision, id: String, mode: TranscriptionMode) {
        Task { @MainActor in
            let (kept, dropped): (Bool, Int)
            if decision.keep { (kept, dropped) = await SpoolStore.shared.keep(id) }
            else { await SpoolStore.shared.delete(id); (kept, dropped) = (false, 0) }
            for toast in decision.toasts { showSpoolToast(toast, kept: kept, mode: mode) }
            if dropped > 0 { showToast("Oldest saved recording discarded") }
            await refreshRetryList()
        }
    }

    @MainActor private func showSpoolToast(_ toast: SpoolToast, kept: Bool, mode: TranscriptionMode) {
        let saved = kept ? " Audio saved under Retry." : ""
        switch toast {
        case .copiedNotPasted: showToast("No app to paste into — the text is on the clipboard.")
        case .possiblyTruncated: showToast("The transcript may be cut off.\(saved)")
        case .notSavedOnServer: showToast("The server did not save this dictation.\(saved)")
        case .failed(let kind):
            AppNotifications.notify(title: "Dictation Failed", body: Self.failureMessage(kind, mode: mode) + saved)
        }
    }

    /// Launch: snapshot the spool ids now (later dictations have new ids),
    /// save their audio to `retry/` off the main thread, never send anything.
    @MainActor func recoverSpoolAtLaunch() {
        let ids = SpoolFiles.spoolIDs()
        Task { @MainActor in
            let n = await SpoolStore.shared.recover(ids)
            if n > 0 { showToast("\(n) saved recording\(n == 1 ? "" : "s") from last session — see Retry") }
            await refreshRetryList()
        }
    }

    @MainActor func refreshRetryList() async {
        recordingState.retryItems = await SpoolStore.shared.list()
    }

    /// Retry → clipboard + last text. The store decides whether the entry goes.
    @MainActor func retrySaved(_ id: String) {
        let mode = Settings.shared.mode
        recordingState.retryStatus = "Retrying…"
        Task { @MainActor in
            do {
                let result = try await SpoolStore.shared.retry(id, mode: mode)
                LastTextStore.save(result, mode: mode)
                ClipboardInjector.copyOnly(result.text)
                recordingState.retryStatus = "Copied — \(result.text.count.formatted(.number)) chars"
                    + (result.serverSaved ? "" : ". The server did not save it; kept under Retry.")
            } catch {
                Log.warning("retry failed: \(ErrorLog.describe(error))", category: "dictation")
                let outcome = ErrorLog.outcome(error)
                recordingState.retryStatus = error is RetryError
                    ? "This recording cannot be retried in this mode."
                    : Self.failureMessage(DictationOutcome(outcome).failureKind, mode: mode)
            }
            await refreshRetryList()
        }
    }

    @MainActor func exportSaved(_ item: RetryItem) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "WisprAlt recording.\(item.audioURL.pathExtension)"
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let dest = panel.url else { return }
        Task { @MainActor in
            do {
                try await SpoolStore.shared.export(item.id, to: dest)
                recordingState.retryStatus = "Exported."
            } catch {
                Log.warning("export failed: \(ErrorLog.describe(error))", category: "dictation")
                recordingState.retryStatus = "Export failed."
            }
        }
    }

    @MainActor func discardSaved(_ id: String) {
        Task { @MainActor in
            await SpoolStore.shared.discard(id)
            recordingState.retryStatus = nil
            await refreshRetryList()
        }
    }
}

private extension DictationOutcome {
    var failureKind: FailureKind {
        if case .failed(let k) = self { return k }
        return .other
    }
}
