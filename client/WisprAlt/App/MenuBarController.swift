import AppKit
import Combine
import SwiftUI

private extension Duration {
    /// Convert a `Duration` (returned by `ContinuousClock` arithmetic) into
    /// floating-point milliseconds for log lines.
    var milliseconds: Double {
        let comps = self.components
        return Double(comps.seconds) * 1_000.0 + Double(comps.attoseconds) * 1e-15
    }
}

// MARK: - RecordingState

/// Lightweight ObservableObject that carries upload-progress state into the SwiftUI
/// popover. `MenuBarController` owns the single instance and mutates it on the main
/// actor; SwiftUI views observe it via `@EnvironmentObject`.
final class RecordingState: ObservableObject {
    /// Upload fraction in [0.0, 1.0]. Drives `RecordingIndicatorView(.uploading(_))`.
    @Published var uploadFraction: Double = 0
}

// MARK: - MenuBarController

/// Controls the menubar status item, popover, and the app's recording mode state machine.
///
/// This class is intentionally NSObject only — not ObservableObject — because it
/// drives AppKit directly. SwiftUI views that need state observe Settings or
/// other ObservableObjects injected from AppDelegate.
///
/// Mic mutual exclusion (v3 delta):
///   `tryStartDictation()` returns false and logs a warning toast if `isMeetingActive` is true.
///   The `meetingActive` flag is kept in sync with `MeetingRecorder.shared.isActive`.
final class MenuBarController: NSObject {
    // MARK: - Mode state machine

    /// Represents all valid UI states for the menubar icon and popover.
    enum Mode {
        case idle
        case dictating
        case meetingRecording
        case uploading
        case processing
        case done
    }

    var mode: Mode = .idle {
        didSet { updateIcon() }
    }

    // MARK: - Meeting active flag
    // Kept in sync with MeetingRecorder.shared.isActive.
    // Exposed as `isMeetingActive` computed property so the rest of the app
    // reads a stable interface.
    private var meetingActive: Bool = false

    /// True when a meeting recording is in progress. Used for mic mutual exclusion.
    var isMeetingActive: Bool { meetingActive }

    // MARK: - Sparkle update error

    /// Non-nil when the most recent Sparkle update cycle aborted with an error.
    /// Cleared when the user retries the update check.
    var lastUpdateError: String? = nil

    // MARK: - Recording state (observed by RecordingIndicatorView)

    let recordingState = RecordingState()

    // MARK: - Owned recorders

    /// Owned dictation recorder — created once and reused across dictation sessions.
    private let dictationRecorder = DictationRecorder()

    // MARK: - Meeting filename rename support
    private var meetingRecordingStart: Date?
    private var meetingStartFileURL: URL?

    // MARK: - Status item

    private let statusItem: NSStatusItem

    // MARK: - Popover hosting SettingsView

    private let popover = NSPopover()

    // MARK: - First-launch dialog

    /// Standalone window hosting `DisplayNameSheet`. Reused across present cycles.
    /// We present a separate NSWindow (not an NSPopover .sheet) because the
    /// popover + .sheet combination is broken on macOS 15.
    private var firstLaunchWindow: NSWindow?

    /// Combine subscriptions; held for the controller's lifetime.
    private var cancellables: Set<AnyCancellable> = []

    // MARK: - Init

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()

        configureStatusItem()
        configurePopover()
        updateIcon()
        configureMeetingCapObservers()
        configureFirstLaunchObserver()
    }

    /// Subscribe to `FirstLaunchCoordinator.shared.$isPresentingNameSheet` so the
    /// standalone NSWindow shows/hides in lockstep with the coordinator's state.
    ///
    /// `FirstLaunchCoordinator` is `@MainActor`-isolated, so we hop onto the main
    /// actor to subscribe. Hop is one-shot at init; subsequent sink fires already
    /// run on main since the publisher's source mutations are main-isolated.
    private func configureFirstLaunchObserver() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            FirstLaunchCoordinator.shared.$isPresentingNameSheet
                .removeDuplicates()
                .sink { [weak self] isPresented in
                    if isPresented {
                        self?.presentFirstLaunchNameWindow()
                    } else {
                        self?.firstLaunchWindow?.close()
                    }
                }
                .store(in: &self.cancellables)
        }
    }

    /// Display the first-launch name sheet as a standalone window.
    /// Avoids NSPopover + SwiftUI .sheet incompatibility on macOS 15.
    private func presentFirstLaunchNameWindow() {
        if firstLaunchWindow == nil {
            let host = NSHostingController(
                rootView: DisplayNameSheet()
                    .environmentObject(FirstLaunchCoordinator.shared)
            )
            let win = NSWindow(contentViewController: host)
            win.title = "Welcome to WisprAlt"
            win.styleMask = [.titled, .closable]
            win.isReleasedWhenClosed = false
            win.center()
            win.level = .floating  // keep above other windows
            firstLaunchWindow = win
        }
        firstLaunchWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - C13: Recording cap observers

    private func configureMeetingCapObservers() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleMeetingMaxDurationReached),
            name: .meetingMaxDurationReached,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleMeetingApproachingCap),
            name: .meetingApproachingCap,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleDictationConfigChanged),
            name: .dictationConfigChanged,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleMeetingConfigChanged),
            name: .meetingConfigChanged,
            object: nil
        )
    }

    /// Fires when the audio input device changes mid-dictation (AirPods plugged
    /// in/out, default input switched). The recorder has already invalidated
    /// its tap — we just need to reset the menubar state to idle and surface a
    /// brief notification so the user knows to retry.
    @objc private func handleDictationConfigChanged() {
        Task { @MainActor in
            guard self.mode == .dictating else { return }
            Log.info(
                "MenuBarController: aborting in-flight dictation — audio device changed.",
                category: "dictation"
            )
            // Best-effort cleanup: stop the recorder if it's still running.
            // We discard whatever partial WAV exists; transcribing it would
            // fail anyway (engine state is invalid).
            Task.detached {
                _ = try? await self.dictationRecorder.stop()
            }
            self.mode = .idle
            AppNotifications.notify(
                title: "Dictation Cancelled",
                body: "Audio input device changed mid-recording. Press FN again to retry."
            )
        }
    }

    /// Fires when the CoreAudio HAL default input device changes mid-meeting
    /// recording. SCStream emits no equivalent callback, so AudioDeviceListener
    /// in MeetingRecorder detects this and posts .meetingConfigChanged.
    ///
    /// We snapshot `lastOutputURL` BEFORE calling stop() because SCStream's
    /// `didStopWithError` may have already flipped `isActive` to false, causing
    /// stop() to throw `.notRunning`. The partial WAV is still on disk in that
    /// case and must be cleaned up.
    @objc private func handleMeetingConfigChanged() {
        Task { @MainActor in
            guard self.mode == .meetingRecording else { return }
            Log.info(
                "MenuBarController: aborting in-flight meeting recording — audio input device changed.",
                category: "capture"
            )
            // Snapshot the URL before stop() so we can delete the partial WAV
            // even if SCStream's didStopWithError already flipped isActive=false.
            let partialURL = MeetingRecorder.shared.lastOutputURL

            do {
                _ = try await MeetingRecorder.shared.stop()
            } catch {
                Log.warning(
                    "Meeting config-change abort: stop() threw \(error)",
                    category: "capture"
                )
            }

            // Delete the partial WAV — an interrupted meeting is not a valid recording.
            // Containment check: only delete if the URL is inside the configured
            // meetings directory. This prevents UserDefaults poisoning (or any
            // future bug that lets a wrong URL leak into MeetingRecorder.lastOutputURL)
            // from turning this cleanup into a write-anywhere primitive.
            // Codex review caught this.
            if let url = partialURL {
                let meetingsDir = Settings.shared.meetingsPath.standardizedFileURL.path
                let target = url.standardizedFileURL.path
                if target.hasPrefix(meetingsDir + "/") {
                    try? FileManager.default.removeItem(at: url)
                } else {
                    Log.warning(
                        "Meeting config-change abort: refused to delete partial WAV at \(target) (outside \(meetingsDir))",
                        category: "capture"
                    )
                }
            }

            self.meetingActive = false
            self.mode = .idle

            AppNotifications.notify(
                title: "Meeting Cancelled",
                body: "Audio input device changed mid-recording. Triple-tap FN to start a new meeting."
            )
        }
    }

    @objc private func handleMeetingMaxDurationReached() {
        guard MeetingRecorder.shared.isActive else { return }
        let capMin = Settings.shared.maxMeetingMinutes
        Log.info("Meeting max duration reached (\(capMin) min) — stopping and uploading.", category: "meeting")
        AppNotifications.notify(
            title: "Meeting Recording Stopped",
            body: "\(capMin)-minute cap reached. Uploading now."
        )
        // toggleMeetingRecording is @MainActor; this NotificationCenter callback
        // is nonisolated. Hop to the main actor before invoking.
        Task { @MainActor in
            self.toggleMeetingRecording()
        }
    }

    @objc private func handleMeetingApproachingCap() {
        let capMin = Settings.shared.maxMeetingMinutes
        Log.info("Meeting approaching \(capMin)-minute cap (60 min elapsed).", category: "meeting")
        AppNotifications.notify(
            title: "Meeting Recording",
            body: "60 minutes elapsed; maximum recording length is \(capMin) minutes."
        )
    }

    // MARK: - Configuration

    private func configureStatusItem() {
        if let button = statusItem.button {
            button.action = #selector(handleStatusItemClick(_:))
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    private func configurePopover() {
        popover.behavior = .transient
        popover.contentViewController = NSHostingController(
            rootView: SettingsView()
                .environmentObject(Settings.shared)
                .environmentObject(recordingState)
        )
    }

    // MARK: - Icon update

    private func updateIcon() {
        guard let button = statusItem.button else { return }

        switch mode {
        case .meetingRecording:
            let composite = renderRecComposite()
            button.image = composite
            button.contentTintColor = nil
            button.attributedTitle = NSAttributedString(string: "")
            button.title = ""
            button.imagePosition = .imageOnly
            button.toolTip = "WisprAlt — Meeting Recording"

        default:
            let (symbolName, accessibilityLabel): (String, String) = {
                switch mode {
                case .idle:             return ("mic", "WisprAlt — Idle")
                case .dictating:        return ("mic.fill", "WisprAlt — Dictating")
                case .uploading:        return ("icloud.and.arrow.up", "WisprAlt — Uploading")
                case .processing:       return ("waveform", "WisprAlt — Processing")
                case .done:             return ("checkmark.circle", "WisprAlt — Done")
                case .meetingRecording: return ("mic", "WisprAlt")  // unreachable
                }
            }()
            let image = NSImage(
                systemSymbolName: symbolName,
                accessibilityDescription: accessibilityLabel
            )
            image?.isTemplate = true
            button.image = image
            button.contentTintColor = nil
            button.attributedTitle = NSAttributedString(string: "")
            button.title = ""
            button.imagePosition = .imageOnly
            button.toolTip = accessibilityLabel
        }
    }

    // MARK: - Popover toggle

    @objc private func handleStatusItemClick(_ sender: NSStatusBarButton) {
        if popover.isShown {
            popover.performClose(sender)
        } else if let button = statusItem.button {
            popover.show(
                relativeTo: button.bounds,
                of: button,
                preferredEdge: .minY
            )
            // Bring app to front so the popover keyboard-focuses correctly.
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    // MARK: - Mic mutual exclusion (v3 delta)

    /// Attempt to start dictation. Returns false and logs a warning toast if a meeting is active.
    @discardableResult
    func tryStartDictation() -> Bool {
        guard !isMeetingActive else {
            Log.warning(
                "Dictation start ignored — meeting recording is active.",
                category: "dictation"
            )
            showToast("Dictation unavailable while meeting recording is active.")
            return false
        }
        return true
    }

    // MARK: - Meeting recording control

    private func startMeetingRecording() {
        let now = Date()
        let startName = humanReadableMeetingFilename(start: now, end: nil, in: Settings.shared.meetingsPath)
        let outputURL = Settings.shared.meetingsPath.appendingPathComponent(startName)
        self.meetingRecordingStart = now
        self.meetingStartFileURL = outputURL

        Task { @MainActor in
            do {
                try await MeetingRecorder.shared.start(to: outputURL)
                meetingActive = true
                mode = .meetingRecording
                Log.info("Meeting recording started → \(startName)", category: "meeting")
            } catch {
                Log.error("Failed to start meeting recording: \(error.localizedDescription)", category: "meeting")
                AppNotifications.notify(title: "Meeting Recording Failed", body: error.localizedDescription)
            }
        }
    }

    private func stopMeetingRecording() {
        Task { @MainActor in
            do {
                let wavURL = try await MeetingRecorder.shared.stop()
                let endDate = Date()
                let humanName = humanReadableMeetingFilename(
                    start: meetingRecordingStart ?? endDate,
                    end: endDate,
                    in: Settings.shared.meetingsPath
                )
                let renamedURL = Settings.shared.meetingsPath.appendingPathComponent(humanName)
                let finalURL: URL
                do {
                    try FileManager.default.moveItem(at: wavURL, to: renamedURL)
                    finalURL = renamedURL
                    Log.info("Meeting WAV renamed → \(humanName)", category: "meeting")
                } catch {
                    Log.warning("Could not rename meeting WAV: \(error.localizedDescription). Using start-only name.", category: "meeting")
                    finalURL = wavURL
                }
                meetingActive = false
                mode = .uploading
                recordingState.uploadFraction = 0
                Log.info("Meeting recording stopped — uploading \(finalURL.lastPathComponent)", category: "meeting")

                await processMeetingUpload(wavURL: finalURL)
            } catch {
                meetingActive = false
                mode = .idle
                Log.error("Failed to stop meeting recording: \(error.localizedDescription)", category: "meeting")
                AppNotifications.notify(title: "Meeting Recording Error", body: error.localizedDescription)
            }
        }
    }

    /// Uploads, polls, downloads, and finalises a completed meeting WAV.
    private func processMeetingUpload(wavURL: URL) async {
        let baseName = wavURL.deletingPathExtension().lastPathComponent
        let baseURL = Settings.shared.meetingsPath.appendingPathComponent(baseName)

        do {
            // --- Upload ---
            // Estimate recording duration from file size (2-ch 16kHz Float32 = 128 kB/s).
            let fileSize = (try? FileManager.default.attributesOfItem(atPath: wavURL.path)[.size] as? Int) ?? 0
            let estimatedDurationSeconds = Double(fileSize) / (2 * 16_000 * 4)  // 2ch * 16kHz * 4 bytes

            let jobID = try await MeetingAPI.submit(wavURL) { [weak self] fraction in
                guard let self else { return }
                self.recordingState.uploadFraction = fraction
            }

            // --- Processing ---
            mode = .processing
            Log.info("Meeting uploaded — job_id: \(jobID), polling for completion.", category: "meeting")

            // C11: compute a deadline — allow at least 2× the recording duration or 600s,
            // whichever is larger. If the deadline expires, give up and notify the user.
            let pollDeadline = Date(timeIntervalSinceNow: max(2 * estimatedDurationSeconds, 600))

            // Poll every 5 seconds until done, failed, or deadline exceeded.
            // Capture the `outputs` map from the done response for format-aware downloads.
            var outputFormats: [String] = []
            pollLoop: while true {
                if Date() > pollDeadline {
                    // Server did not respond in time; clean up and surface error.
                    Log.error("Meeting poll timed out for job \(jobID) — deadline exceeded.", category: "meeting")
                    try? await MeetingAPI.delete(jobID)
                    throw MeetingProcessingError.pollTimedOut
                }
                try await Task.sleep(nanoseconds: 5_000_000_000)
                let status = try await MeetingAPI.poll(jobID)
                switch status {
                case .done(let outputs):
                    outputFormats = Array(outputs.keys)
                    break pollLoop
                case .failed(let reason):
                    throw MeetingProcessingError.serverFailed(reason)
                case .pending, .running:
                    continue
                }
            }

            // --- Download all formats ---
            // Use the server-supplied `outputs` keys (sorted for deterministic ordering)
            // so future server-side format additions are tracked automatically.
            // Fall back to the hardcoded list if the server returned an empty outputs map.
            let formatsToDownload: [String]
            if !outputFormats.isEmpty {
                formatsToDownload = outputFormats.sorted()
            } else {
                formatsToDownload = ["json", "srt", "vtt", "txt"]
            }
            for fmt in formatsToDownload {
                let data = try await MeetingAPI.download(jobID, format: fmt)
                try data.write(to: baseURL.appendingPathExtension(fmt), options: .atomic)
            }

            // --- Cleanup ---
            try await MeetingAPI.delete(jobID)
            TranscriptStore.shared.refresh()

            AppNotifications.notify(title: "Meeting transcribed", body: baseName)
            Log.info("Meeting transcription complete — \(baseName)", category: "meeting")

            mode = .done
            try await Task.sleep(nanoseconds: 3_000_000_000)
            mode = .idle

        } catch {
            mode = .idle
            let message: String
            if case ServerError.unauthorized = error {
                message = "Authentication failed — re-paste your API key in Settings."
            } else {
                message = error.localizedDescription
            }
            Log.error("Meeting processing failed: \(message)", category: "meeting")
            AppNotifications.notify(title: "Meeting Transcription Failed", body: message)
        }
    }

    // MARK: - Composite REC icon

    private func renderRecComposite() -> NSImage {
        let dotSize: CGFloat = 8
        let dotGap: CGFloat = 3
        let verticalPadding: CGFloat = 1  // descender clearance
        let font = NSFont.systemFont(ofSize: 11, weight: .bold)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.systemRed,
        ]
        let text = NSAttributedString(string: "REC", attributes: attrs)
        let textSize = text.size()
        let canvasHeight = ceil(textSize.height) + verticalPadding * 2
        let canvasWidth = dotSize + dotGap + ceil(textSize.width) + 2
        let img = NSImage(
            size: NSSize(width: canvasWidth, height: canvasHeight),
            flipped: false
        ) { _ in
            let rect = NSRect(x: 0, y: 0, width: canvasWidth, height: canvasHeight)
            let dotRect = NSRect(
                x: 0,
                y: (rect.height - dotSize) / 2,
                width: dotSize,
                height: dotSize
            )
            NSColor.systemRed.setFill()
            NSBezierPath(ovalIn: dotRect).fill()
            text.draw(in: NSRect(
                x: dotSize + dotGap,
                y: verticalPadding,
                width: ceil(textSize.width),
                height: ceil(textSize.height)
            ))
            return true
        }
        img.isTemplate = false  // pre-rendered red, not a template
        return img
    }

    // MARK: - Human-readable meeting filename

    private func humanReadableMeetingFilename(start: Date, end: Date?, in dir: URL) -> String {
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.dateFormat = "EEE MMM d"

        let timeFormatter = DateFormatter()
        timeFormatter.locale = Locale(identifier: "en_US_POSIX")
        timeFormatter.amSymbol = "am"
        timeFormatter.pmSymbol = "pm"
        // Periods, not colons (filesystem-friendly across rsync to Linux, zip, etc.)
        // No seconds — user wants the readable form "3.05-5.20pm".
        // Collision is handled below by appending " (2)" / " (3)" if needed.
        timeFormatter.dateFormat = "h.mma"

        let day = dayFormatter.string(from: start)
        let startTime = timeFormatter.string(from: start)
        let base: String
        if let end = end {
            let endTime = timeFormatter.string(from: end)
            base = "\(day) \(startTime)-\(endTime)"
        } else {
            base = "\(day) \(startTime)"
        }

        // Collision guard: check the base name against ALL sidecar extensions.
        let exts = ["wav", "json", "srt", "vtt", "txt"]
        func anyExists(_ baseName: String) -> Bool {
            for ext in exts {
                if FileManager.default.fileExists(atPath: dir.appendingPathComponent("\(baseName).\(ext)").path) {
                    return true
                }
            }
            return false
        }
        var name = base
        var i = 2
        while anyExists(name) {
            name = "\(base) (\(i))"
            i += 1
        }
        return "\(name).wav"
    }

    // MARK: - Toast helper

    /// Shows a brief user-visible warning via AppNotifications.
    private func showToast(_ message: String) {
        Log.warning(message, category: "ui")
        AppNotifications.notify(title: "WisprAlt", body: message)
    }
}

// MARK: - FNKeyEventsDelegate

extension MenuBarController: FNKeyEventsDelegate {

    /// Called on the main actor by FNKeyMonitor when FN has been held ≥ holdThreshold.
    @MainActor func dictationStart() {
        guard tryStartDictation() else { return }
        mode = .dictating
        do {
            try dictationRecorder.start()
            Log.info("Dictation started.", category: "dictation")
        } catch {
            mode = .idle
            Log.error("DictationRecorder failed to start: \(error.localizedDescription)", category: "dictation")
        }
    }

    /// Called on the main actor by FNKeyMonitor when FN is released after a confirmed hold.
    @MainActor func dictationStop() {
        guard mode == .dictating else { return }
        // Set idle immediately so the icon stops flashing.
        mode = .idle

        Task { @MainActor in
            // Latency breakdown timestamps — surfaces the ~3-5s multi-sentence
            // hunch by isolating network+upload+inference vs AX-inject. Filter
            // OSLog with: `log show --last 5m --predicate 'subsystem == "co.wispralt"
            // AND category == "dictation"' --style compact --info`
            //
            // ContinuousClock is monotonic (cannot go backwards on NTP correction);
            // Date() was wall-clock and could produce negative latencies during
            // background `timed` adjustments. Issued at Log.debug so the per-
            // dictation chatter is off-by-default; flip via OSLog profile when
            // measuring.
            let clock = ContinuousClock()
            let tStopStart = clock.now
            do {
                let wavData = try await dictationRecorder.stop()
                let stopMs = (clock.now - tStopStart).milliseconds
                Log.debug(
                    "dictation/timing: stop_ms=\(String(format: "%.1f", stopMs)) bytes=\(wavData.count)",
                    category: "dictation"
                )

                let tNet = clock.now
                let text = try await DictationAPI.transcribe(wavData)
                let netMs = (clock.now - tNet).milliseconds
                Log.debug(
                    "dictation/timing: net_total_ms=\(String(format: "%.1f", netMs)) chars=\(text.count)",
                    category: "dictation"
                )

                let tInj = clock.now
                TextInjector.inject(text)
                let injMs = (clock.now - tInj).milliseconds
                let totalMs = (clock.now - tStopStart).milliseconds
                Log.debug(
                    "dictation/timing: inject_ms=\(String(format: "%.1f", injMs)) total_ms=\(String(format: "%.1f", totalMs))",
                    category: "dictation"
                )
                Log.info("Dictation injected: \"\(text.prefix(60))\"", category: "dictation")

            } catch ServerError.unauthorized {
                Log.error("Dictation failed — unauthorized. Re-paste API key in Settings.", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Failed",
                    body: "API key rejected. Re-paste your API key in Settings."
                )
            } catch DictationRecorder.DictationError.emptyRecording {
                // FN tapped without speaking, or mic returned silence.
                // Don't notify — would be noisy on accidental taps.
                Log.info("Dictation: empty recording (no audio captured).", category: "dictation")
            } catch DictationRecorder.DictationError.writeFailed(let underlying) {
                Log.error("Dictation failed — file write error: \(underlying)", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Failed",
                    body: "Could not write audio to disk: \(underlying.localizedDescription)"
                )
            } catch DictationRecorder.DictationError.meetingRecordingActive {
                Log.info("Dictation suppressed — meeting recording is active.", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Unavailable",
                    body: "A meeting is recording — release the meeting first."
                )
            } catch {
                Log.error("Dictation failed: \(error.localizedDescription)", category: "dictation")
                AppNotifications.notify(title: "Dictation Failed", body: error.localizedDescription)
            }
        }
    }

    /// Called on the main actor by FNKeyMonitor on triple-tap.
    @MainActor func toggleMeetingRecording() {
        if MeetingRecorder.shared.isActive {
            stopMeetingRecording()
        } else {
            startMeetingRecording()
        }
    }
}

// MARK: - Private error types

private enum MeetingProcessingError: Error, LocalizedError {
    case serverFailed(String)
    case pollTimedOut

    var errorDescription: String? {
        switch self {
        case .serverFailed(let reason):
            return "Server-side processing failed: \(reason)"
        case .pollTimedOut:
            return "Server didn't respond in time; check /metrics for job status."
        }
    }
}
