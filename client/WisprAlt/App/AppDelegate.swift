import AppKit
import ServiceManagement
import WisprAltCore

/// Root application delegate.
///
/// Responsibilities:
///  - Owns the MenuBarController (strong reference; keeps it alive for the app lifetime).
///  - Owns the SparkleController (strong reference; Sparkle requires long-lived host).
///  - Owns the FNKeyMonitor (strong reference; tap must live for the app lifetime).
///  - On launch: runs PermissionGate.checkAll() asynchronously, then starts FNKeyMonitor.
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var shared: AppDelegate?

    // MARK: - Owned controllers (strong references)

    private(set) var menuBarController: MenuBarController!
    private var sparkleController: SparkleController!
    /// Retains the CGEvent tap for the entire app lifetime.
    private var fnKeyMonitor: FNKeyMonitor!

    // MARK: - NSApplicationDelegate

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppDelegate.shared = self  // FIRST — before anything else accesses .shared
        Log.info("WisprAlt launching.", category: "app")

        // Ask the user once for permission to post local notifications. Without
        // this call every UNUserNotificationCenter request silently fails on
        // macOS 13+ (secure-field skip notification, file-transcription results, …).
        // Idempotent — macOS suppresses re-prompts after the first decision.
        AppNotifications.requestAuthorization()

        // Register for launch-at-login via SMAppService ON FIRST LAUNCH ONLY,
        // gated by a UserDefaults flag so the user's later "off" toggle persists.
        //
        // Without this gate, every app launch would auto-register, silently undoing
        // a user who turned the Settings toggle off (Codex review found this — the
        // toggle could never stay disabled across app restarts).
        //
        // On first launch the gate fires once, creates the System Settings entry,
        // sets the flag, and never auto-registers again. From then on, the in-app
        // toggle in SettingsView is the sole controller via Settings.launchAtLogin.
        let didAutoRegisterKey = "co.wispralt.didAutoRegisterLoginItem"
        let suite = UserDefaults(suiteName: "co.wispralt.WisprAlt") ?? .standard
        if !suite.bool(forKey: didAutoRegisterKey) {
            let service = SMAppService.mainApp
            do {
                switch service.status {
                case .notRegistered, .notFound:
                    try service.register()
                    Log.info("SMAppService: registered for launch at login (first launch).", category: "lifecycle")
                case .enabled:
                    Log.info("SMAppService: already enabled (skipping first-launch register).", category: "lifecycle")
                case .requiresApproval:
                    Log.warning(
                        "SMAppService: requires approval in System Settings → Login Items.",
                        category: "lifecycle"
                    )
                @unknown default:
                    break
                }
                suite.set(true, forKey: didAutoRegisterKey)
            } catch {
                Log.warning(
                    "SMAppService.register() failed: \(ErrorLog.describe(error)). Verify the app is signed with Apple Development identity.",
                    category: "lifecycle"
                )
                // Do NOT set the flag on failure so we retry next launch.
            }
        }

        // Instantiate the menubar controller first so the status item is visible immediately.
        menuBarController = MenuBarController()

        // Saved audio from the last session (crash / quit mid-dictation) goes
        // to Retry. The spool ids are snapshotted now, before the FN monitor
        // exists, so no new dictation is ever touched. Nothing is sent.
        menuBarController.recoverSpoolAtLaunch()

        // Warm the OpenAI key cache (at most one Keychain prompt per signed build).
        _ = OpenAIKey.current()

        // Launch decision (Local mode only): resume polling a Local file job
        // the last process left in flight, and GET /me once to mirror
        // display_name and maybe ask for a name. Online contacts neither.
        Task { @MainActor in
            menuBarController.applyModeEvent(.launch)
        }

        // Sparkle auto-update controller (honours the isBusy update gate).
        sparkleController = SparkleController()
        sparkleController.menuBarController = menuBarController

        // Run the sequential 3-permission wizard asynchronously so the menubar item
        // appears before the first TCC dialog.
        Task { @MainActor in
            let results = await PermissionGate.checkAll()
            logPermissionResults(results)

            // Install FN key monitor after permissions are checked.
            // start(delegate:) will log a warning if Input Monitoring is not yet granted;
            // the user will be prompted to restart after granting via PermissionGate.
            self.fnKeyMonitor = FNKeyMonitor()
            self.fnKeyMonitor.start(delegate: self.menuBarController)
        }

        // v0.5.0: poll GitHub Releases 60s after launch to surface available updates
        // via the menubar dot + Settings → Updates section.
        UpdateChecker.shared.checkSoon()
    }

    func applicationWillTerminate(_ notification: Notification) {
        Log.info("WisprAlt terminating.", category: "app")
        fnKeyMonitor?.stop()
    }

    // MARK: - Private helpers

    private func logPermissionResults(_ results: [PermissionStatus]) {
        let labels = ["Accessibility", "Input Monitoring", "Microphone"]
        for (i, status) in results.enumerated() {
            let label = i < labels.count ? labels[i] : "Permission \(i + 1)"
            switch status {
            case .granted:
                Log.info("\(label): granted.", category: "permissions")
            case .denied:
                Log.warning("\(label): denied.", category: "permissions")
            case .unknown:
                Log.warning("\(label): unknown status.", category: "permissions")
            }
        }
    }
}
