import Combine
import Foundation
import SwiftUI
import WisprAltCore

/// Observable view-model that watches a folder for filesystem changes and
/// publishes a relative-time caption (e.g. "just now", "12s ago", "3m ago")
/// describing when the most recently-relevant transcript was last modified.
///
/// Backed by:
///   - `DispatchSource.makeFileSystemObjectSource` on the watched folder
///     (instant updates on direct-child create/rename/delete);
///   - a 10 s `Timer` that bumps `tick` so the relative-time string advances
///     while the popover is visible without any file event;
///   - a `NotificationCenter` observer on `.wisprAltTranscriptWritten` so
///     transcript writes that land in *subfolders* (which the parent watcher
///     doesn't see) still trigger an immediate refresh.
///
/// Lifecycle: caller invokes `start()` from `.onAppear` and `stop()` from
/// `.onDisappear`. Both are idempotent. The captured file descriptor is owned
/// by the DispatchSource cancel handler — `stop()` does NOT close it directly.
@MainActor
final class LastTranscriptCaptionViewModel: ObservableObject {
    @Published private(set) var lastModified: Date?
    /// Bumped by the 10 s timer so SwiftUI re-renders the relative-time string
    /// even when no filesystem event has fired.
    @Published private(set) var tick: Int = 0

    private var source: DispatchSourceFileSystemObject?
    private var fd: Int32 = -1
    private var timer: AnyCancellable?
    private var notifObserver: NSObjectProtocol?

    private let folderURL: URL
    private let lookup: @MainActor () -> Date?

    init(folderURL: URL, lookup: @escaping @MainActor () -> Date?) {
        self.folderURL = folderURL
        self.lookup = lookup
    }

    func start() {
        guard source == nil else { return }
        try? FileManager.default.createDirectory(
            at: folderURL,
            withIntermediateDirectories: true
        )

        fd = open(folderURL.path, O_EVTONLY)
        guard fd >= 0 else {
            Log.warning(
                "LastTranscriptCaptionViewModel: open() failed for transcripts folder",
                category: "captions"
            )
            refresh()
            return
        }

        let s = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete],
            queue: .main
        )
        s.setEventHandler { [weak self] in self?.refresh() }
        // Cancel handler owns the descriptor's lifetime — do NOT also close it
        // from stop(). DispatchSource.cancel() returns immediately; the cancel
        // handler runs asynchronously when the source has fully torn down.
        s.setCancelHandler { [fd] in close(fd) }
        s.resume()
        source = s

        timer = Timer.publish(every: 10.0, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in self?.tick &+= 1 }

        notifObserver = NotificationCenter.default.addObserver(
            forName: .wisprAltTranscriptWritten,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }

        refresh()
    }

    func stop() {
        timer?.cancel()
        timer = nil
        source?.cancel()
        source = nil
        if let token = notifObserver {
            NotificationCenter.default.removeObserver(token)
            notifObserver = nil
        }
        // Do NOT close(fd) here — the cancel handler owns it.
        fd = -1
    }

    private func refresh() {
        lastModified = lookup()
    }

    var captionText: String {
        // Reference `tick` so SwiftUI re-evaluates this property when the
        // timer fires (otherwise the relative-time string would stick).
        _ = tick
        guard let d = lastModified else { return "No transcripts yet" }
        return Self.relativeOrAbsolute(d)
    }

    static func relativeOrAbsolute(_ date: Date, now: Date = .now) -> String {
        let s = Int(now.timeIntervalSince(date))
        switch s {
        case ..<5:
            return "just now"
        case 5..<60:
            return "\(s)s ago"
        case 60..<3_600:
            return "\(s / 60)m ago"
        case 3_600..<86_400:
            return "\(s / 3_600)h ago"
        default:
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            if Calendar.current.isDateInYesterday(date) {
                f.dateFormat = "'Yesterday' HH:mm"
            } else {
                f.dateFormat = "MMM d HH:mm"
            }
            return f.string(from: date)
        }
    }

    /// Best-effort cleanup if the owner forgot to call `stop()`. We can't hop
    /// to the main actor from `deinit`, but `DispatchSource.cancel()` is
    /// thread-safe and the cancel handler will close the captured descriptor.
    deinit {
        source?.cancel()
    }
}

/// Tiny SwiftUI subview that renders the caption from a view-model owned by
/// the parent (`QuickActionsSection`).
struct LastTranscriptCaption: View {
    @ObservedObject var viewModel: LastTranscriptCaptionViewModel

    var body: some View {
        Text(viewModel.captionText)
            .font(.caption)
            .foregroundStyle(.secondary)
    }
}

// MARK: - Smart formatting status

extension LastTranscriptCaption {
    /// Short, fixed label for a Mercury outcome (safe to log: never text).
    static func formattingLabel(_ f: Formatting) -> String {
        switch f {
        case .off: return "off"
        case .applied: return "applied"
        case .skipped(let why): return "skipped(\(why))"
        case .failed(let why): return "failed(\(why))"
        }
    }

    /// The caption under the Smart formatting toggle for the last dictation.
    static func formattingStatus(_ f: Formatting?) -> String? {
        switch f {
        case nil, .off?: return nil
        case .applied?: return "Last dictation: formatted."
        case .skipped("short")?: return "Last dictation: too short to format (under \(MercuryPolicy.minWords) words)."
        case .skipped?: return "Last dictation: not formatted — add your OpenRouter key."
        case .failed(let why)?: return "Last dictation: formatting \(why == "ratio" ? "rejected (changed too much)" : "failed (\(why))"); raw text used."
        }
    }
}
