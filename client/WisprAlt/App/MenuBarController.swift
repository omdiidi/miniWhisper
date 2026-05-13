import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

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

    // MARK: - Per-phase progress (server-driven)

    /// Raw phase name from the server (`ffprobe`, `transcribe`, `diarize`, …).
    @Published var phase: String? = nil
    /// Friendly phase label sent by the server (`"Transcribing"`, …). Falls
    /// back to the local map via `phaseLabelDisplay` when absent.
    @Published var phaseLabel: String? = nil
    /// 1-based chunk progress during `transcribe`. Nil outside that phase.
    @Published var chunkIndex: Int? = nil
    /// Total chunk count during `transcribe`. Nil outside that phase.
    @Published var totalChunks: Int? = nil
    /// Seconds since the current phase started (server-side wall clock).
    @Published var phaseElapsedS: Double? = nil
    /// Total audio duration in seconds, populated after server-side ffprobe.
    @Published var audioDurationS: Double? = nil
    /// Currently-active server job id. Persisted to UserDefaults so a process
    /// restart can resume polling. Nil when no job is in flight.
    @Published var activeJobID: String? = nil
    /// Set when the user cancelled but the server-side executor is still
    /// running an in-pipeline phase. The popover shows a "Previous job
    /// finishing" banner and blocks new submissions while this is non-nil.
    @Published var serverFinishingJobID: String? = nil

    /// Reset every per-job field. Caller is responsible for also clearing
    /// the UserDefaults `activeJobID` key — `reset()` only touches in-memory
    /// state so it can be called from contexts that shouldn't write defaults.
    func reset() {
        uploadFraction = 0
        phase = nil
        phaseLabel = nil
        chunkIndex = nil
        totalChunks = nil
        phaseElapsedS = nil
        audioDurationS = nil
        activeJobID = nil
    }

    /// Friendly phase label: uses the server-provided `phaseLabel` when set,
    /// otherwise looks up `phase` in a static map; falls back to the raw
    /// phase name. Nil only when no phase has been observed yet.
    var phaseLabelDisplay: String? {
        if let server = phaseLabel, !server.isEmpty { return server }
        guard let p = phase else { return nil }
        return Self.phaseLabelMap[p] ?? p
    }

    private static let phaseLabelMap: [String: String] = [
        "queued": "Waiting in queue",
        "starting": "Starting",
        "ffprobe": "Inspecting audio",
        "ffmpeg_decode": "Decoding audio",
        "transcribe_load": "Loading transcription model",
        "transcribe": "Transcribing",
        "diarize_load": "Loading speaker model",
        "diarize": "Identifying speakers",
        "merge": "Finalizing transcript",
        "output_write": "Writing outputs",
    ]
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
        case converting
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

    /// Token returned by the block-based `addObserver(forName:...)` for the
    /// `NSApplication.didResignActiveNotification` popover-auto-close handler.
    /// Held so we can remove it on deinit; otherwise a recreated controller
    /// (in tests, previews, or future relaunch flows) would accumulate stale
    /// observers with no cleanup path.
    private var didResignActiveObserver: NSObjectProtocol?

    // MARK: - Pending-uploads drain triggers

    /// Process-local last-shown timestamp for debounced toasts. 10-min window
    /// per kind so flapping connectivity doesn't spam the user.
    private enum ToastKind: String {
        case meetingOffline
        case fallbackUnavailable
    }
    private var lastToastShown: [ToastKind: Date] = [:]
    private static let toastDebounceSec: TimeInterval = 600

    /// Held so the timer survives across foreground/background transitions.
    private var pendingUploadsDrainTimer: Timer?
    /// Held to remove the foreground observer on deinit.
    private var didBecomeActiveObserver: NSObjectProtocol?

    // MARK: - Active-job task handles

    /// Strong handle to the currently-running `runFileTranscriptionJob` task.
    /// `cancelActiveTranscription()` calls `.cancel()` on this to abort the
    /// poll loop. Nil between jobs.
    private var activeJobTask: Task<Void, Error>? = nil
    /// Strong handle to the URLSession used for the in-flight upload. We hold
    /// it so `cancelActiveTranscription()` can call `invalidateAndCancel()` to
    /// abort an upload mid-flight (the network bytes are dropped immediately;
    /// no server-side state is created until the upload completes).
    private var activeUploadSession: URLSession? = nil

    /// UserDefaults key for the persisted in-flight job id. Read in
    /// `resumeInFlightJobIfNeeded()` on app launch so a process restart can
    /// re-attach to a server-side job that's still running.
    private static let activeJobIDDefaultsKey = "wispralt.activeJobID"

    // MARK: - Init

    /// Process-wide weak reference so SwiftUI views (which receive only
    /// `@EnvironmentObject` injections of `Settings` / `RecordingState`) can
    /// reach the controller for entry points like `transcribePickedFile()`.
    /// Set in `init`; cleared automatically when the controller deallocates.
    static weak var shared: MenuBarController?

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()
        MenuBarController.shared = self

        configureStatusItem()
        configurePopover()
        updateIcon()
        configureMeetingCapObservers()
        configureFirstLaunchObserver()
        configurePendingUploadsDrainTriggers()
    }

    /// Wire up the three automatic drain triggers for `PendingUploadsQueue`:
    ///   - app foreground (`NSApplication.didBecomeActiveNotification`)
    ///   - 120s repeating timer
    ///   (the third trigger is "successful dictation" — fired inline from
    ///    `dictationStop()` after the inject lands.)
    ///
    /// All three coalesce inside `PendingUploadsQueue.drain()` via the
    /// drain coordinator so concurrent triggers don't stack up.
    private func configurePendingUploadsDrainTriggers() {
        // Foreground trigger.
        didBecomeActiveObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { _ in
            Task.detached(priority: .utility) {
                await PendingUploadsQueue.shared.drain()
            }
        }

        // 120s timer trigger. `Timer.scheduledTimer` adds itself to the
        // current run loop in `.common` mode so it survives event-tracking
        // (e.g. user holding down a menu).
        pendingUploadsDrainTimer = Timer.scheduledTimer(withTimeInterval: 120, repeats: true) { _ in
            Task.detached(priority: .utility) {
                await PendingUploadsQueue.shared.drain()
            }
        }
        if let timer = pendingUploadsDrainTimer {
            RunLoop.main.add(timer, forMode: .common)
        }
    }

    /// Fire the meeting-offline toast, debounced per-kind (10 min cooldown).
    /// Wording is the brief's locked phrasing.
    @MainActor
    fileprivate func showOfflineMeetingToast() {
        guard shouldShowToast(.meetingOffline) else { return }
        AppNotifications.notify(
            title: "WisprAlt — Server Offline",
            body: "Meeting will upload when it's back. Recording is saved locally."
        )
    }

    /// Fire the dictation-fallback-unavailable toast (Worker also down).
    @MainActor
    fileprivate func showFallbackUnavailableToast() {
        guard shouldShowToast(.fallbackUnavailable) else { return }
        AppNotifications.notify(
            title: "WisprAlt",
            body: "Transcription temporarily unavailable."
        )
    }

    private func shouldShowToast(_ kind: ToastKind) -> Bool {
        let now = Date()
        if let last = lastToastShown[kind],
           now.timeIntervalSince(last) < Self.toastDebounceSec
        {
            return false
        }
        lastToastShown[kind] = now
        return true
    }

    /// Hook reserved for a future menubar `(N pending)` indicator. Currently
    /// a no-op — the status item uses a transient popover, not a menu, so
    /// surfacing per-state badges requires a popover-side overhaul outside
    /// the scope of the offline-fallback feature.
    fileprivate func refreshPendingMenu() {}

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
            win.delegate = self  // sync window-close back to coordinator state
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

    /// Build a fresh hosting controller for the popover's root view. Called
    /// every time the popover is shown so SwiftUI state (scroll position,
    /// expanded sections, focus) resets to its default. Reusing a single
    /// NSHostingController across opens persists that state — undesired here.
    private func makePopoverRootViewController() -> NSViewController {
        NSHostingController(
            rootView: SettingsView()
                .environmentObject(Settings.shared)
                .environmentObject(recordingState)
        )
    }

    private func configurePopover() {
        popover.behavior = .transient
        popover.contentViewController = makePopoverRootViewController()
        // Belt-and-suspenders: NSPopover.behavior = .transient is supposed to
        // auto-close on click-outside, but in LSUIElement (menubar-only) apps
        // the popover sometimes stays open when the user clicks into another
        // app because the WisprAlt process never had key focus to lose. Force
        // close on any app-deactivation event. Token is stored on the controller
        // so deinit can remove it (matters if MenuBarController is ever recreated).
        didResignActiveObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            if self.popover.isShown {
                self.popover.performClose(nil)
            }
        }
    }

    deinit {
        if let token = didResignActiveObserver {
            NotificationCenter.default.removeObserver(token)
        }
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
                case .converting:       return ("arrow.triangle.2.circlepath", "WisprAlt — Converting…")
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
            // Replace the hosting controller on every open so SwiftUI state
            // (scroll position, expanded sections, focus) resets to its
            // initial values. Any in-flight sheet on the prior controller
            // closes — that is the intended reset behavior.
            popover.contentViewController = makePopoverRootViewController()
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

    /// Uploads, polls, downloads, and finalises a completed meeting m4a.
    ///
    /// Post-Task-10: meetings are now AAC m4a containers and route through
    /// `POST /transcribe/file` via `runFileTranscriptionJob`. The legacy
    /// `/transcribe/meeting` POST is reserved for `PendingUploadsQueue`'s
    /// dual-replay of any pre-Task-9 `.wav` entries that are still queued
    /// from before the upgrade.
    @MainActor
    private func processMeetingUpload(wavURL: URL) async {
        let baseName = wavURL.deletingPathExtension().lastPathComponent

        // Record the upload-attempt start so the offline-signature classifier
        // can compute elapsed time on the failure path. Used only when the
        // catch branch fires.
        let uploadStartedAt = Date()

        do {
            try await runFileTranscriptionJob(
                sourceURL: wavURL,
                outputDirectory: Settings.shared.meetingsPath,
                stem: baseName,
                mode: "meeting"
            )

            TranscriptStore.shared.refresh()
            NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)

            AppNotifications.notify(title: "Meeting transcribed", body: baseName)
            Log.info("Meeting transcription complete — \(baseName)", category: "meeting")

            mode = .done
            try await Task.sleep(nanoseconds: 3_000_000_000)
            mode = .idle

        } catch {
            mode = .idle

            // Offline-signature check: when the mini is unreachable we queue
            // the recording locally for later retry rather than surfacing a
            // generic "transcription failed" error. Diarization can't run on
            // the cloud fallback, so meetings never proxy to OpenRouter.
            let attempt = Self.buildMeetingAttempt(error: error, startedAt: uploadStartedAt)
            if ServerClient.shared.isOfflineSignature(attempt) {
                do {
                    try PendingUploadsQueue.shared.enqueue(wav: wavURL)
                    showOfflineMeetingToast()
                    refreshPendingMenu()
                    Log.warning(
                        "Meeting upload offline-signature confirmed — queued \(baseName) locally.",
                        category: "fallback"
                    )
                } catch {
                    Log.error(
                        "Meeting offline AND queue.enqueue failed: \(error.localizedDescription)",
                        category: "fallback"
                    )
                    AppNotifications.notify(
                        title: "Meeting Save Failed",
                        body: "Server offline and could not save recording locally."
                    )
                }
                return
            }

            let message = formatTranscriptionError(error)
            Log.error("Meeting processing failed: \(message)", category: "meeting")
            AppNotifications.notify(title: "Meeting Transcription Failed", body: message)
        }
    }

    /// Probe a source file's audio duration via AVAsset. Returns nil if the
    /// container is unreadable or has no audio track. Used to size the
    /// poll deadline in `runFileTranscriptionJob`.
    private static func probeAudioDuration(_ url: URL) async -> TimeInterval? {
        let asset = AVURLAsset(url: url)
        do {
            let duration = try await asset.load(.duration)
            let seconds = CMTimeGetSeconds(duration)
            guard seconds.isFinite, seconds > 0 else { return nil }
            return seconds
        } catch {
            return nil
        }
    }

    /// Submit an original-container source file via `POST /transcribe/file`,
    /// poll until done, download every reported format to
    /// `<outputDirectory>/<stem>.<fmt>`, delete the server-side job.
    /// Throws on failure; never enqueues for offline retry — caller decides.
    ///
    /// Deadline scales with the source's audio duration (probed via AVAsset)
    /// so a 5-minute clip and a 4-hour meeting both get realistic budgets.
    /// Multiplier accounts for WhisperX + Pyannote on Mac mini M4 running
    /// at roughly 0.3-0.5× realtime for stereo diarization. On timeout we
    /// LEAVE the server-side job in place so the user can recover/poll it
    /// later — deleting it would throw away an in-flight transcription.
    /// Used by both the meeting upload path (`processMeetingUpload`) and
    /// the custom-transcription path (`processCustomTranscriptionUpload`).
    @MainActor
    private func runFileTranscriptionJob(
        sourceURL: URL,
        outputDirectory: URL,
        stem: String,
        mode: String
    ) async throws {
        // Probe audio duration BEFORE upload so we can size the poll deadline.
        // Falls back to a generous 4-hour ceiling if probing fails (corrupt
        // header, unsupported container) — server will reject upload anyway
        // if it really can't decode.
        let durationSeconds = await Self.probeAudioDuration(sourceURL)
        let deadlineSeconds: TimeInterval
        if let dur = durationSeconds {
            // 3× realtime + 5 min overhead, floor 600s, ceiling 6h.
            deadlineSeconds = max(600, min(6 * 3600, dur * 3 + 300))
            Log.info("File deadline sized: duration=\(Int(dur))s → poll budget=\(Int(deadlineSeconds))s", category: "transcribe")
        } else {
            deadlineSeconds = 4 * 3600
            Log.warning("File duration probe failed — falling back to 4h poll budget.", category: "transcribe")
        }

        // --- Upload ---
        let jobID = try await MeetingAPI.submitFile(
            sourceURL,
            mode: mode,
            progress: { [weak self] fraction in
                guard let self else { return }
                self.recordingState.uploadFraction = fraction
            },
            sessionRegistered: { [weak self] session in
                // The continuation closure that creates the URLSession is
                // @Sendable; hop to the main actor to satisfy isolation on
                // the controller's `activeUploadSession` field.
                Task { @MainActor [weak self] in
                    self?.activeUploadSession = session
                }
            }
        )
        // Upload finished cleanly — drop the session reference. Cancel paths
        // also clear this in `cancelActiveTranscription()`.
        activeUploadSession = nil
        // Make the job id observable + persistent so a process restart can
        // resume polling and the UI can render Cancel + View-server-log.
        recordingState.activeJobID = jobID.raw
        UserDefaults.standard.set(jobID.raw, forKey: Self.activeJobIDDefaultsKey)

        // --- Processing ---
        self.mode = .processing
        self.recordingState.uploadFraction = 0  // clear so "Uploading 100%" stops showing
        Log.info("File uploaded — job_id: \(jobID), polling for completion.", category: "transcribe")

        let pollDeadline = Date(timeIntervalSinceNow: deadlineSeconds)

        var outputFormats: [String] = []
        pollLoop: while true {
            if Date() > pollDeadline {
                Log.error("File poll timed out for job \(jobID) after \(Int(deadlineSeconds))s — server-side job left in place for manual recovery.", category: "transcribe")
                throw MeetingProcessingError.pollTimedOut
            }
            try await Task.sleep(nanoseconds: 5_000_000_000)
            let status = try await pollWithBackoff(jobID)
            switch status {
            case .done(let outputs):
                outputFormats = Array(outputs.keys)
                break pollLoop
            case .failed(let reason):
                throw MeetingProcessingError.serverFailed(reason)
            case .pending:
                continue
            case .running(let progress, let serverFinishing):
                recordingState.phase = progress?.phase
                recordingState.phaseLabel = progress?.phaseLabel
                recordingState.chunkIndex = progress?.chunkIndex
                recordingState.totalChunks = progress?.totalChunks
                recordingState.phaseElapsedS = progress?.phaseElapsedS
                recordingState.audioDurationS = progress?.audioDurationS
                if serverFinishing {
                    // User-initiated cancel was acknowledged on the server,
                    // but an in-pipeline phase is still running. Surface this
                    // through the popover banner.
                    recordingState.serverFinishingJobID = recordingState.activeJobID
                }
                continue
            }
        }
        // Job completed cleanly — clear per-job UI state. The download +
        // delete steps below are quick and don't need progress fields.
        recordingState.phase = nil
        recordingState.phaseLabel = nil
        recordingState.chunkIndex = nil
        recordingState.totalChunks = nil

        // --- Download all formats ---
        let formatsToDownload: [String]
        if !outputFormats.isEmpty {
            formatsToDownload = outputFormats.sorted()
        } else {
            formatsToDownload = ["json", "srt", "vtt", "txt"]
        }
        let baseURL = outputDirectory.appendingPathComponent(stem)
        for fmt in formatsToDownload {
            let data = try await MeetingAPI.download(jobID, format: fmt)
            try data.write(to: baseURL.appendingPathExtension(fmt), options: .atomic)
        }

        // --- Cleanup ---
        try await MeetingAPI.delete(jobID)
        recordingState.activeJobID = nil
        UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
    }

    /// Poll the job with exponential backoff (1 s / 2 s / 4 s) on transient
    /// `URLError`s — offline blips and timeouts should not surface as an
    /// immediate processing failure. After 3 consecutive transient failures
    /// the underlying error is re-thrown so the upper-level catch can route
    /// to the offline queue or surface the failure to the user.
    @MainActor
    private func pollWithBackoff(_ jobID: JobID) async throws -> JobStatus {
        let backoffsNs: [UInt64] = [1_000_000_000, 2_000_000_000, 4_000_000_000]
        var lastError: Error?
        for attempt in 0..<(backoffsNs.count + 1) {
            do {
                return try await MeetingAPI.poll(jobID)
            } catch let urlError as URLError {
                lastError = urlError
                Log.warning(
                    "Poll transient error (attempt \(attempt + 1)): \(urlError.code.rawValue) — backing off.",
                    category: "transcribe"
                )
            } catch ServerError.transport(let underlying) {
                lastError = underlying
                Log.warning(
                    "Poll transport error (attempt \(attempt + 1)): \(underlying.localizedDescription) — backing off.",
                    category: "transcribe"
                )
            }
            if attempt < backoffsNs.count {
                try await Task.sleep(nanoseconds: backoffsNs[attempt])
            }
        }
        throw lastError ?? ServerError.transport(URLError(.unknown))
    }

    // MARK: - Cancel + resume

    /// Cancel whatever transcription is currently running. Mid-upload cancel
    /// is clean (URLSession is invalidated, no server-side state created).
    /// Mid-transcribe cancel is *advisory* — the server-side executor thread
    /// keeps running until it finishes naturally; `serverFinishingJobID` is
    /// set on `recordingState` so the popover banner appears.
    @MainActor
    func cancelActiveTranscription() async {
        Log.info("Cancel requested — tearing down active transcription.", category: "transcribe")

        // 1) Cancel the in-process Task — stops the poll loop on the next
        //    cooperative yield point.
        activeJobTask?.cancel()
        activeJobTask = nil

        // 2) Drop the upload session, if any — kills any in-flight bytes.
        activeUploadSession?.invalidateAndCancel()
        activeUploadSession = nil

        // 3) Tell the server to stop. The DELETE is best-effort; on success
        //    we set `serverFinishingJobID` so the UI knows the cancel was
        //    acknowledged (and that in-pipeline work may continue).
        if let raw = recordingState.activeJobID {
            let jobID = JobID(raw: raw)
            do {
                try await MeetingAPI.cancel(jobID)
                // Move the id from "active" to "server finishing" so the
                // popover banner appears and new submissions are blocked.
                // Mid-transcribe cancel is advisory — the server may still
                // be running an in-pipeline phase that cannot be interrupted.
                recordingState.serverFinishingJobID = raw
                Log.warning(
                    "Cancel is advisory for in-pipeline phases — server may keep running until natural completion.",
                    category: "transcribe"
                )
            } catch {
                Log.warning(
                    "MeetingAPI.cancel(\(raw)) failed: \(error.localizedDescription)",
                    category: "transcribe"
                )
            }
        }

        // 4) Reset local UI state and forget the persisted id. The
        //    serverFinishingJobID (if set) is preserved on recordingState so
        //    the banner stays visible.
        recordingState.activeJobID = nil
        UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
        recordingState.phase = nil
        recordingState.phaseLabel = nil
        recordingState.chunkIndex = nil
        recordingState.totalChunks = nil
        recordingState.phaseElapsedS = nil
        recordingState.uploadFraction = 0
        self.mode = .idle
    }

    /// On app launch: if UserDefaults remembers an in-flight job id, set it
    /// on `recordingState` so the popover renders Cancel + View-server-log,
    /// and spawn a polling Task that drains the job to completion (or
    /// surfaces `serverFinishingJobID` if it's still grinding).
    ///
    /// Called from `AppDelegate.applicationDidFinishLaunching` after
    /// `MenuBarController` is constructed. Safe to call when no id is stored
    /// — early-returns silently.
    @MainActor
    func resumeInFlightJobIfNeeded() {
        guard let raw = UserDefaults.standard.string(forKey: Self.activeJobIDDefaultsKey),
              !raw.isEmpty else { return }
        Log.info("Resuming polling for in-flight job: \(raw)", category: "transcribe")
        recordingState.activeJobID = raw
        self.mode = .processing
        self.recordingState.uploadFraction = 0  // clear so "Uploading 100%" stops showing
        activeJobTask = Task { @MainActor [weak self] in
            await self?.resumePollingForJob(JobID(raw: raw))
        }
    }

    /// Drain a recovered job. Polls until done/failed and writes a synthetic
    /// "resumed" notification on completion. No download is performed —
    /// recovery is best-effort; the user can manually open the meetings
    /// folder if outputs were written elsewhere.
    ///
    /// Note: a resumed job has lost its caller-known `outputDirectory` and
    /// `stem`. We surface completion to the user but the actual downloads
    /// stay on the server until the user triggers a fresh transcription
    /// (or until the server's GC sweeps them). This is intentional — the
    /// recovery path is for visibility, not for completing the original
    /// transaction.
    @MainActor
    private func resumePollingForJob(_ jobID: JobID) async {
        let deadlineSeconds: TimeInterval = 6 * 3600
        let pollDeadline = Date(timeIntervalSinceNow: deadlineSeconds)
        do {
            while true {
                if Date() > pollDeadline {
                    Log.warning("Resumed-job poll timed out for \(jobID).", category: "transcribe")
                    break
                }
                try await Task.sleep(nanoseconds: 5_000_000_000)
                let status = try await pollWithBackoff(jobID)
                switch status {
                case .done:
                    AppNotifications.notify(
                        title: "Transcription Recovered",
                        body: "A job that was running when WisprAlt restarted has completed. Re-run the transcription to download outputs."
                    )
                    recordingState.activeJobID = nil
                    UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
                    self.mode = .idle
                    return
                case .failed(let reason):
                    Log.warning("Resumed-job failed: \(reason)", category: "transcribe")
                    recordingState.activeJobID = nil
                    UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
                    self.mode = .idle
                    return
                case .pending:
                    continue
                case .running(let progress, let serverFinishing):
                    recordingState.phase = progress?.phase
                    recordingState.phaseLabel = progress?.phaseLabel
                    recordingState.chunkIndex = progress?.chunkIndex
                    recordingState.totalChunks = progress?.totalChunks
                    recordingState.phaseElapsedS = progress?.phaseElapsedS
                    recordingState.audioDurationS = progress?.audioDurationS
                    if serverFinishing {
                        recordingState.serverFinishingJobID = jobID.raw
                    }
                    continue
                }
            }
        } catch {
            Log.warning(
                "Resumed-job polling aborted: \(error.localizedDescription)",
                category: "transcribe"
            )
        }
    }

    /// Map a thrown transcription error to a user-facing message. Shared
    /// between the meeting and custom-transcription catch paths so
    /// `ServerError.unauthorized` always surfaces the same actionable hint.
    @MainActor
    private func formatTranscriptionError(_ error: Error) -> String {
        if case ServerError.unauthorized = error {
            return "Authentication failed — re-paste your API key in Settings."
        }
        return error.localizedDescription
    }

    // MARK: - Custom transcription (file-pick) flow

    /// Public entry point invoked from the SwiftUI "Transcribe file…" button.
    /// Activates the app, presents an `NSOpenPanel`, and on user selection
    /// hands off to the async pipeline.
    @MainActor
    func transcribePickedFile() {
        // Bring the app to front so the modal sheet appears reliably above
        // the menubar popover (which is .transient and will dismiss).
        NSApp.activate(ignoringOtherApps: true)

        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        let exts = ["mp3", "m4a", "wav", "aac", "mp4", "mov", "m4v", "caf", "aiff", "flac"]
        let extTypes = exts.compactMap { UTType(filenameExtension: $0) }
        panel.allowedContentTypes = [.audio, .movie] + extTypes
        panel.title = "Choose audio or video to transcribe"
        panel.prompt = "Transcribe"

        guard panel.runModal() == .OK, let picked = panel.url else { return }

        // Decouple the panel cleanup from the async pipeline.
        Task { @MainActor [weak self] in
            await self?.handlePickedFile(picked)
        }
    }

    /// Drives transcode → upload for a picked audio/video file. Sets `mode`
    /// transitions and surfaces any failure as a notification.
    @MainActor
    private func handlePickedFile(_ picked: URL) async {
        let stem = picked.deletingPathExtension().lastPathComponent

        mode = .converting

        let subdir: URL
        do {
            subdir = try CustomTranscriptionsStore.makeJobDirectory(forStem: stem)
        } catch {
            let message = formatTranscriptionError(error)
            Log.error("Custom transcription: makeJobDirectory failed: \(message)", category: "transcribe")
            AppNotifications.notify(title: "Custom Transcription Failed", body: message)
            mode = .idle
            return
        }

        // Copy the picked file as-is into the per-job folder. The server now
        // ffmpeg-decodes the original container; no client-side transcoding.
        let originalDestination = subdir.appendingPathComponent(picked.lastPathComponent)
        do {
            try FileManager.default.copyItem(at: picked, to: originalDestination)
        } catch {
            let message = formatTranscriptionError(error)
            Log.error("Custom transcription: copy source file failed: \(message)", category: "transcribe")
            AppNotifications.notify(title: "Custom Transcription Failed", body: message)
            try? FileManager.default.removeItem(at: subdir)
            mode = .idle
            return
        }

        await processCustomTranscriptionUpload(
            sourceURL: originalDestination,
            outputDirectory: subdir,
            stem: stem
        )
    }

    /// Uploads a custom-transcription source file (original container, no
    /// client-side transcoding) via `POST /transcribe/file`. Mirrors
    /// `processMeetingUpload` but skips the offline-queue path: custom
    /// transcriptions are always user-initiated and don't get retried later.
    @MainActor
    private func processCustomTranscriptionUpload(
        sourceURL: URL,
        outputDirectory: URL,
        stem: String
    ) async {
        mode = .uploading
        recordingState.uploadFraction = 0

        do {
            try await runFileTranscriptionJob(
                sourceURL: sourceURL,
                outputDirectory: outputDirectory,
                stem: stem,
                mode: "file"
            )

            // Harmless even though custom transcripts live in subfolders the
            // store doesn't index — keeps consistency with the meeting path.
            TranscriptStore.shared.refresh()
            NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)

            AppNotifications.notify(title: "Custom Transcription Complete", body: stem)
            Log.info("Custom transcription complete — \(stem)", category: "transcribe")

            mode = .done
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            mode = .idle
        } catch {
            let message = formatTranscriptionError(error)
            Log.error("Custom transcription failed: \(message)", category: "transcribe")
            AppNotifications.notify(title: "Custom Transcription Failed", body: message)
            mode = .idle
        }
    }

    /// Build a `ServerClient.RequestAttempt` from a thrown meeting-upload
    /// error so the offline-signature classifier can run on the catch path.
    /// `MeetingAPI.submit` builds its own URLSession, so it doesn't go
    /// through `ServerClient.execute` — we synthesize the attempt here.
    private static func buildMeetingAttempt(
        error: Error,
        startedAt: Date
    ) -> ServerClient.RequestAttempt {
        let finishedAt = Date()
        let outcome: ServerClient.RequestAttempt.Outcome
        if let urlErr = error as? URLError {
            outcome = .error(urlErr)
        } else if case ServerError.transport(let underlying) = error {
            outcome = .error(underlying)
        } else if case ServerError.server(let status, _) = error,
                  let url = Settings.shared.serverURL,
                  let synthetic = HTTPURLResponse(
                      url: url,
                      statusCode: status,
                      httpVersion: "HTTP/1.1",
                      headerFields: ["X-Request-Id": "synthetic"]
                  )
        {
            // Synthetic origin response — has X-Request-Id, so classifier
            // refuses to fall back. That's correct: an origin 4xx/5xx is
            // not a tunnel-level failure.
            outcome = .response(synthetic)
        } else {
            outcome = .error(error)
        }
        return ServerClient.RequestAttempt(
            startedAt: startedAt,
            finishedAt: finishedAt,
            lastByteSentAt: nil,
            outcome: outcome
        )
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
        // `wav` is kept in the collision list (alongside `m4a`) so a fresh m4a
        // recording does not stomp legacy WAV meetings sharing the same minute.
        let exts = ["m4a", "wav", "json", "srt", "vtt", "txt"]
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
        return "\(name).m4a"
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

        // Capture the user's intended target window NOW — before the network
        // round-trip (which can take 10-20s on slow uplinks) gives them time
        // to switch focus. TextInjector activates this PID before resolving
        // the AX element so dictation lands where they were when they
        // finished speaking, not where focus drifts during the upload.
        let targetPID: pid_t? = NSWorkspace.shared.frontmostApplication?.processIdentifier

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
                await TextInjector.inject(text, targetPID: targetPID)
                let injMs = (clock.now - tInj).milliseconds
                let totalMs = (clock.now - tStopStart).milliseconds
                Log.debug(
                    "dictation/timing: inject_ms=\(String(format: "%.1f", injMs)) total_ms=\(String(format: "%.1f", totalMs))",
                    category: "dictation"
                )
                Log.info("Dictation injected: \"\(text.prefix(60))\"", category: "dictation")

                // Successful dictation is one of the four PendingUploadsQueue
                // drain triggers. Detached + utility priority so it doesn't
                // contend with the next dictation if the user FN-holds again
                // immediately.
                Task.detached(priority: .utility) {
                    await PendingUploadsQueue.shared.drain()
                }

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
                // When the fallback path itself fails (mini AND Worker
                // unreachable, or Worker rate-limited / budget-exhausted),
                // surface a debounced "transcription temporarily unavailable"
                // toast instead of a noisy localized-description.
                if case ServerError.server = error {
                    showFallbackUnavailableToast()
                }
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

// MARK: - NSWindowDelegate (first-launch name sheet)

extension MenuBarController: NSWindowDelegate {
    /// When the user closes the first-launch name window via the title-bar close
    /// button, sync that intent back to the coordinator (treat as Skip).
    ///
    /// `windowWillClose` ALSO fires on programmatic close — when the coordinator's
    /// `recordSave()` or `recordSkip()` flips `isPresentingNameSheet` to false and
    /// the Combine sink calls `window.close()`. In that case, the coordinator
    /// already updated its state and we must NOT call `recordSkip()` again — that
    /// would clobber the success path (overwriting the cleared skip flag with a
    /// fresh 30-day suppression and re-classifying a successful save as a skip).
    ///
    /// Distinguish: if `isPresentingNameSheet == true` at close time, it's a
    /// user-initiated title-bar dismiss. If already `false`, recordSkip/recordSave
    /// already ran — leave coordinator state alone.
    nonisolated func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow else { return }
        Task { @MainActor [weak self] in
            guard let self, closing === self.firstLaunchWindow else { return }
            if FirstLaunchCoordinator.shared.isPresentingNameSheet {
                FirstLaunchCoordinator.shared.recordSkip()
            }
        }
    }
}
