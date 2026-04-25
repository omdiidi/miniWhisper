import AppKit
import SwiftUI

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

    // MARK: - Recording state (observed by RecordingIndicatorView)

    let recordingState = RecordingState()

    // MARK: - Owned recorders

    /// Owned dictation recorder — created once and reused across dictation sessions.
    private let dictationRecorder = DictationRecorder()

    // MARK: - Status item

    private let statusItem: NSStatusItem

    // MARK: - Popover hosting SettingsView

    private let popover = NSPopover()

    // MARK: - Init

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()

        configureStatusItem()
        configurePopover()
        updateIcon()
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
        let (symbolName, accessibilityLabel): (String, String) = {
            switch mode {
            case .idle:
                return ("mic", "WisprAlt — Idle")
            case .dictating:
                return ("mic.fill", "WisprAlt — Dictating")
            case .meetingRecording:
                return ("record.circle", "WisprAlt — Meeting Recording")
            case .uploading:
                return ("icloud.and.arrow.up", "WisprAlt — Uploading")
            case .processing:
                return ("waveform", "WisprAlt — Processing")
            case .done:
                return ("checkmark.circle", "WisprAlt — Done")
            }
        }()

        let image = NSImage(
            systemSymbolName: symbolName,
            accessibilityDescription: accessibilityLabel
        )
        // Render as template so macOS dark/light mode tints it correctly.
        image?.isTemplate = true
        button.image = image
        button.toolTip = accessibilityLabel
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
        // Build output URL: <meetingsPath>/<YYYY-MM-DD_HHmmZZZZZ>_meeting.wav
        // Strip colons from the timezone offset so the filename is filesystem-safe.
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd_HHmmZZZZZ"
        let rawStamp = formatter.string(from: Date())
        // "2026-04-24_1543-07:00" → "2026-04-24_1543-0700"
        let stamp = rawStamp.replacingOccurrences(of: ":", with: "", range: rawStamp.range(of: ":", options: .backwards))
        let fileName = "\(stamp)_meeting.wav"
        let outputURL = Settings.shared.meetingsPath.appendingPathComponent(fileName)

        Task { @MainActor in
            do {
                try await MeetingRecorder.shared.start(to: outputURL)
                meetingActive = true
                mode = .meetingRecording
                Log.info("Meeting recording started → \(fileName)", category: "meeting")
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
                meetingActive = false
                mode = .uploading
                recordingState.uploadFraction = 0
                Log.info("Meeting recording stopped — uploading \(wavURL.lastPathComponent)", category: "meeting")

                await processMeetingUpload(wavURL: wavURL)
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
            let jobID = try await MeetingAPI.submit(wavURL) { [weak self] fraction in
                guard let self else { return }
                self.recordingState.uploadFraction = fraction
            }

            // --- Processing ---
            mode = .processing
            Log.info("Meeting uploaded — job_id: \(jobID), polling for completion.", category: "meeting")

            // Poll every 5 seconds until done or failed.
            pollLoop: while true {
                try await Task.sleep(nanoseconds: 5_000_000_000)
                let status = try await MeetingAPI.poll(jobID)
                switch status {
                case .done:
                    break pollLoop
                case .failed(let reason):
                    throw MeetingProcessingError.serverFailed(reason)
                case .pending, .running:
                    continue
                }
            }

            // --- Download all four formats ---
            for fmt in ["json", "srt", "vtt", "txt"] {
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
            do {
                let wavData = try await dictationRecorder.stop()
                Log.debug("Dictation stopped — \(wavData.count) bytes, sending to server.", category: "dictation")

                let text = try await DictationAPI.transcribe(wavData)
                TextInjector.inject(text)
                Log.info("Dictation injected: \"\(text.prefix(60))\"", category: "dictation")

            } catch ServerError.unauthorized {
                Log.error("Dictation failed — unauthorized. Re-paste API key in Settings.", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Failed",
                    body: "API key rejected. Re-paste your API key in Settings."
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

    var errorDescription: String? {
        switch self {
        case .serverFailed(let reason):
            return "Server-side processing failed: \(reason)"
        }
    }
}
