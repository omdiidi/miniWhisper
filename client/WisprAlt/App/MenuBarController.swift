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
final class MenuBarController: NSObject {
    // MARK: - Mode state machine

    /// Represents all valid UI states for the menubar icon and popover.
    enum Mode {
        case idle
        case dictating
        case converting
        case uploading
        case processing
        case done
    }

    var mode: Mode = .idle {
        didSet { updateIcon() }
    }

    // MARK: - Busy signal (update gate)

    /// Number of dictation transcription Tasks still running after the FN key
    /// was released. `dictationStop()` increments it BEFORE setting
    /// `mode = .idle` and the spawned Task's first statement is
    /// `defer { dictationsInFlight -= 1 }`, so there is no main-actor turn in
    /// which a finishing dictation reads as not-busy. Main-thread only.
    var dictationsInFlight: Int = 0

    /// True while a dictation is recording or still transcribing/injecting, or
    /// a file upload / server job is in flight. `UpdateChecker` and
    /// `SparkleController` refuse to relaunch the app while this is true, so an
    /// update can never kill a dictation or an in-flight file job.
    var isBusy: Bool {
        [.dictating, .converting, .uploading, .processing].contains(mode)
            || dictationsInFlight > 0
    }

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
        configureObservers()
        configureDictationCapObserver()
        configureFirstLaunchObserver()
    }

    // MARK: - Observers

    func configureObservers() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleDictationConfigChanged),
            name: .dictationConfigChanged,
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
