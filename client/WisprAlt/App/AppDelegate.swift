import AppKit
import ServiceManagement

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
        // macOS 13+ (secure-field skip notification, meeting cap warnings, …).
        // Idempotent — macOS suppresses re-prompts after the first decision.
        AppNotifications.requestAuthorization()

        // Defensive cleanup: an earlier build attempted to override the macOS
        // system default input device for meetings. We dropped that approach
        // in favor of WisprAlt-only mic selection. If a stale crash-recovery
        // key from that build survives in UserDefaults, restore + clear it
        // so the user's system default isn't permanently changed.
        if let savedUID = UserDefaults.standard.string(forKey: "pendingMeetingDefaultInputUID"),
           let savedID = MicEnumerator.audioDeviceID(forUID: savedUID) {
            _ = MicEnumerator.setSystemDefaultInputDevice(savedID)
            Log.warning("Cleaned up stale system-default override from prior build. Restored UID \(savedUID).", category: "audio")
            UserDefaults.standard.removeObject(forKey: "pendingMeetingDefaultInputUID")
        }

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
                    "SMAppService.register() failed: \(error). Verify the app is signed with Apple Development identity.",
                    category: "lifecycle"
                )
                // Do NOT set the flag on failure so we retry next launch.
            }
        }

        // Instantiate the menubar controller first so the status item is visible immediately.
        menuBarController = MenuBarController()

        // Sparkle auto-update controller (honours meeting-guard gate).
        sparkleController = SparkleController()
        sparkleController.menuBarController = menuBarController

        // Best-effort first-launch check: if the user is already configured
        // (server URL set + API key in Keychain), GET /me to mirror display_name
        // locally and possibly present the "What should we call you?" dialog.
        // Skipped silently for fresh installs to avoid a noisy 401.
        //
        // KeychainHelper.getAPIKey() is `throws -> String?` — `try?` produces String??
        // where outer-nil = function threw, inner-nil = no key stored. We .flatMap
        // to collapse to a single Optional so a missing key short-circuits cleanly.
        Task { @MainActor in
            guard Settings.shared.serverURL != nil,
                  let _ = (try? KeychainHelper.getAPIKey()).flatMap({ $0 }) else { return }
            do {
                let me = try await MeAPI.get()
                Settings.shared.displayName = me.display_name
                FirstLaunchCoordinator.shared.maybePresentNameSheet(serverDisplayName: me.display_name)
            } catch {
                Log.debug("display_name check skipped: \(error)", category: "lifecycle")
            }
        }

        // Run the sequential 4-permission wizard asynchronously so the menubar item
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
    }

    func applicationWillTerminate(_ notification: Notification) {
        Log.info("WisprAlt terminating.", category: "app")
        fnKeyMonitor?.stop()
    }

    // MARK: - Private helpers

    private func logPermissionResults(_ results: [PermissionStatus]) {
        let names = ["Accessibility", "Input Monitoring", "Microphone", "Screen Recording"]
        for (i, status) in results.enumerated() {
            let name = i < names.count ? names[i] : "Permission \(i + 1)"
            switch status {
            case .granted:
                Log.info("\(name): granted.", category: "permissions")
            case .denied:
                Log.warning("\(name): denied.", category: "permissions")
            case .unknown:
                Log.warning("\(name): unknown status.", category: "permissions")
            }
        }
    }
}
