import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

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
    var meetingActive: Bool = false

    /// True when a meeting recording is in progress. Used for mic mutual exclusion.
    var isMeetingActive: Bool { meetingActive }

    // MARK: - Sparkle update error

    /// Non-nil when the most recent Sparkle update cycle aborted with an error.
    /// Cleared when the user retries the update check.
    var lastUpdateError: String? = nil

    /// v0.5.0: whether the menubar icon should render with an orange-dot
    /// badge to indicate an available update. Driven by `UpdateChecker`.
    private(set) var updateBadgeVisible: Bool = false

    /// Set/clear the update-available badge. Idempotent and main-thread-only.
    func setUpdateBadge(visible: Bool) {
        guard updateBadgeVisible != visible else { return }
        updateBadgeVisible = visible
        updateIcon()
    }

    // MARK: - Recording state (observed by RecordingIndicatorView)

    let recordingState = RecordingState()

    // MARK: - Owned recorders

    /// Owned dictation recorder — created once and reused across dictation sessions.
    let dictationRecorder = DictationRecorder()

    // MARK: - Meeting filename rename support
    var meetingRecordingStart: Date?
    var meetingStartFileURL: URL?

    // MARK: - Status item

    let statusItem: NSStatusItem

    // MARK: - Popover hosting SettingsView

    let popover = NSPopover()

    // MARK: - First-launch dialog

    /// Standalone window hosting `DisplayNameSheet`. Reused across present cycles.
    /// We present a separate NSWindow (not an NSPopover .sheet) because the
    /// popover + .sheet combination is broken on macOS 15.
    var firstLaunchWindow: NSWindow?

    /// Combine subscriptions; held for the controller's lifetime.
    var cancellables: Set<AnyCancellable> = []

    /// Token returned by the block-based `addObserver(forName:...)` for the
    /// `NSApplication.didResignActiveNotification` popover-auto-close handler.
    /// Held so we can remove it on deinit; otherwise a recreated controller
    /// (in tests, previews, or future relaunch flows) would accumulate stale
    /// observers with no cleanup path.
    var didResignActiveObserver: NSObjectProtocol?

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
    var pendingUploadsDrainTimer: Timer?
    /// Held to remove the foreground observer on deinit.
    var didBecomeActiveObserver: NSObjectProtocol?
    /// Token for the `.dictationMaxDurationReached` observer; removed in deinit.
    var dictationMaxDurationObserver: NSObjectProtocol?

    // MARK: - Active-job task handles

    /// Strong handle to the currently-running `runFileTranscriptionJob` task.
    /// `cancelActiveTranscription()` calls `.cancel()` on this to abort the
    /// poll loop. Nil between jobs.
    var activeJobTask: Task<Void, Error>? = nil
    /// Strong handle to the URLSession used for the in-flight upload. We hold
    /// it so `cancelActiveTranscription()` can call `invalidateAndCancel()` to
    /// abort an upload mid-flight (the network bytes are dropped immediately;
    /// no server-side state is created until the upload completes).
    var activeUploadSession: URLSession? = nil

    /// Watchdog task that polls `recordingState.lastUploadProgressAt` every
    /// 5 seconds and aborts the upload if no progress has been seen for 30
    /// seconds. See `startUploadWatchdog()`. Nil between uploads.
    var uploadWatchdogTask: Task<Void, Never>? = nil

    /// UserDefaults key for the persisted in-flight job id. Read in
    /// `resumeInFlightJobIfNeeded()` on app launch so a process restart can
    /// re-attach to a server-side job that's still running.
    static let activeJobIDDefaultsKey = "wispralt.activeJobID"

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
        configureDictationCapObserver()
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
    func configurePendingUploadsDrainTriggers() {
        // Foreground trigger.
        didBecomeActiveObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { _ in
            Task.detached(priority: .utility) {
                await PendingUploadsQueue.shared.drain()
            }
            // Plan A §A8: cloud-fallback dictation queue shares this
            // foreground trigger so any OpenRouter-served dictations
            // from offline mode sync to the origin as soon as the user
            // refocuses the app.
            Task.detached(priority: .utility) {
                await DictationFallbackQueue.shared.drain()
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
    func showOfflineMeetingToast() {
        guard shouldShowToast(.meetingOffline) else { return }
        AppNotifications.notify(
            title: "WisprAlt — Server Offline",
            body: "Meeting will upload when it's back. Recording is saved locally."
        )
    }

    /// Fire the dictation-fallback-unavailable toast (Worker also down).
    @MainActor
    func showFallbackUnavailableToast() {
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
    func refreshPendingMenu() {}

    // MARK: - C13: Recording cap observers

    func configureMeetingCapObservers() {
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

    deinit {
        if let token = didResignActiveObserver {
            NotificationCenter.default.removeObserver(token)
        }
        if let token = dictationMaxDurationObserver {
            NotificationCenter.default.removeObserver(token)
        }
    }

    // MARK: - Toast helper

    /// Shows a brief user-visible warning via AppNotifications.
    func showToast(_ message: String) {
        Log.warning(message, category: "ui")
        AppNotifications.notify(title: "WisprAlt", body: message)
    }
}
