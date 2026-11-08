import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers
import WisprAltCore

// MARK: - MenuBarController

/// Controls the menubar status item, popover, dictation and file-job state.
///
/// This class is intentionally NSObject only — not ObservableObject — because it
/// drives AppKit directly. SwiftUI views that need state observe Settings or
/// other ObservableObjects injected from AppDelegate.
final class MenuBarController: NSObject {
    // MARK: - Activity state (main thread only)

    /// The menubar icon's states (Core `ActivityIcon`).
    typealias Mode = ActivityIcon

    /// The icon, derived from state. It is never written and never branched
    /// on: dictation and job logic read `currentSession`, `localUploadPhase`
    /// and `onlineJobTask` directly.
    var mode: Mode {
        ActivityState.icon(dictating: currentSession != nil, local: localUploadPhase,
                           onlineJob: onlineJobTask != nil, doneFlash: localDoneFlash)
    }

    /// The dictation being recorded. Set in `dictationStart` only after
    /// `recorder.start` returned true; cleared by `beginRun` (FN-up, cap,
    /// device change). The only "dictation in progress" check. A mode switch
    /// never touches it, so a recording finishes on its FN-press route.
    var currentSession: DictationSession? = nil {
        didSet { updateIcon() }
    }

    /// Phase of the Local file job. Mirrored to `recordingState` for the
    /// popover (mode-picker lock).
    var localUploadPhase: LocalUploadPhase = .none {
        didSet {
            recordingState.localUploadPhase = localUploadPhase
            updateIcon()
        }
    }

    /// The 3 s "done" icon after a Local file job completes.
    var localDoneFlash: Bool = false {
        didSet { updateIcon() }
    }

    /// The Online file job (Online file transcription assigns it). No mode
    /// switch ever cancels it.
    var onlineJobTask: Task<Void, Never>? = nil {
        didSet { updateIcon() }
    }

    /// Poll generation fence. Bumped on every Local job/poller start and every
    /// cancel; the job wrapper and the resume poller write state only while
    /// `PollFence.mayWrite` holds for the generation they captured.
    var pollGeneration: Int = 0

    /// `/me` runs at most once per launch (Local only).
    var meFetchedThisLaunch: Bool = false

    // MARK: - Busy signal (update gate)

    /// Number of dictation transcription Tasks still running after the FN key
    /// was released. `beginRun` increments it in the same main-actor turn that
    /// clears `currentSession`, and the spawned Task's first statement is
    /// `defer { dictationsInFlight -= 1 }`, so there is no turn in which a
    /// finishing dictation reads as not-busy. Main-thread only. Mirrored to
    /// `recordingState` for the popover's "transcribing N"; not an icon state.
    var dictationsInFlight: Int = 0 {
        didSet {
            updateIcon()
            recordingState.dictationsInFlight = dictationsInFlight
        }
    }

    /// Number of file-transcription Tasks (pick → upload → poll → download)
    /// still running. Incremented before the Task is created and decremented
    /// by the Task's own `defer`. Main-thread only.
    var fileJobsInFlight: Int = 0

    /// True while a dictation is recording or still transcribing/injecting, or
    /// a file upload / server job is in flight. `UpdateChecker` and
    /// `SparkleController` refuse to relaunch the app while this is true, so an
    /// update can never kill a dictation or an in-flight file job.
    var isBusy: Bool {
        currentSession != nil || dictationsInFlight > 0 || fileJobsInFlight > 0
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

    /// Strong handle to the running Local job task (pick → upload → poll, or a
    /// resumed poller). `cancelActiveTranscription()` and a switch to Online
    /// cancel it. The task's `defer` clears it only while its generation is
    /// current, so `activeJobTask != nil` means "a Local poller is running".
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
    /// UserDefaults key for that job's `SavedJobOutput` (output folder + stem),
    /// so restart recovery downloads the outputs to the same place. Always
    /// written and removed together with `activeJobIDDefaultsKey`.
    static let activeJobOutputDefaultsKey = "wispralt.activeJobOutput"

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

    // MARK: - Mode switch / launch

    /// Inputs for `LaunchActions.decide`. The Local key is read only in Local
    /// mode (Online never needs it).
    @MainActor var localJobState: LocalJobState {
        let mode = Settings.shared.mode
        let hasServerConfig = mode == .local && Settings.shared.serverURL != nil
            && ((try? KeychainHelper.getAPIKey()) ?? nil)?.isEmpty == false
        let saved = UserDefaults.standard.string(forKey: Self.activeJobIDDefaultsKey)
        return LocalJobState(mode: mode, hasServerConfig: hasServerConfig,
                             hasSavedJobID: saved?.isEmpty == false,
                             pollerRunning: activeJobTask != nil,
                             onlineJobRunning: onlineJobTask != nil,
                             meFetchedThisLaunch: meFetchedThisLaunch)
    }

    /// True while an invite redeem (`POST /invites/redeem`) is in flight.
    /// `transcribePickedFile` refuses a Local file job meanwhile: the redeem
    /// is about to repoint the server URL and key, which would strand it.
    var inviteRedeemInFlight = false

    /// The one "Local file job active or saved" predicate: converting,
    /// uploading, polled, or its id saved for resume. While it is true an
    /// invite is refused and Settings locks the Local server URL and API key
    /// (either change would point that job at another server and strand it).
    @MainActor var localFileJobActiveOrSaved: Bool {
        let saved = UserDefaults.standard.string(forKey: Self.activeJobIDDefaultsKey)
        return InviteLink.refusedForLocalJob(phase: localUploadPhase, pollerRunning: activeJobTask != nil,
                                             hasSavedJobID: saved?.isEmpty == false)
    }

    /// Runs the launch / mode-switch decision. Called by `AppDelegate` at
    /// launch and by `applyModeSwitch(to:)`.
    @MainActor func applyModeEvent(_ event: ModeEvent) {
        let actions = LaunchActions.decide(event, localJobState)
        if actions.cancelLocalPoll { pauseLocalPoll() }
        if actions.resumeLocalJob { resumeInFlightJobIfNeeded() }
        if actions.fetchMe {
            meFetchedThisLaunch = true
            Task { @MainActor in await fetchMeAndMaybeAskName() }
        }
    }

    /// The popover's mode picker. Refused while a Local upload has no job id
    /// yet (the picker is disabled then too). Never touches `currentSession`
    /// or `onlineJobTask`.
    @MainActor func applyModeSwitch(to mode: TranscriptionMode) {
        guard Settings.shared.mode != mode, !localUploadPhase.locksModePicker else { return }
        Settings.shared.mode = mode
        Log.info("Mode switched to \(mode.rawValue).", category: "app")
        applyModeEvent(.switched(to: mode))
    }

    /// Switch to Online: stop watching the Local job without cancelling it on
    /// the server. The saved `activeJobID` stays, so a switch back (or the
    /// next launch in Local) resumes polling it.
    @MainActor func pauseLocalPoll() {
        pollGeneration += 1
        activeJobTask?.cancel()
        activeJobTask = nil
        recordingState.reset()
        localUploadPhase = .none
        localDoneFlash = false
    }

    /// `GET /me` (Local only, once per launch): mirror `display_name` and
    /// maybe ask for a name.
    @MainActor func fetchMeAndMaybeAskName() async {
        do {
            let me = try await MeAPI.get()
            Settings.shared.displayName = me.display_name
            FirstLaunchCoordinator.shared.maybePresentNameSheet(serverDisplayName: me.display_name)
        } catch {
            Log.debug("display_name check skipped: \(ErrorLog.describe(error))", category: "lifecycle")
        }
    }

    /// Online FN press with no key: open the popover at the OpenAI key field.
    @MainActor func openKeyEntry() {
        recordingState.keyEntryRequested = true
        if !popover.isShown { showPopover() }
    }
}
