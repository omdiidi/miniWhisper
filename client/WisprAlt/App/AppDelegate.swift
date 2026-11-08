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
    /// A `wispralt://join` link delivered before launch finished.
    private var pendingInviteURL: URL?

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

        // An Online file job interrupted by quit is lost (never resumed); its
        // temp dir is swept here, before any job can start.
        OnlineFileJob.sweepLeftovers()

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

        if let url = pendingInviteURL {
            pendingInviteURL = nil
            DispatchQueue.main.async { self.handleInvite(url) }
        }
    }

    // MARK: - Invite links (wispralt://join, issued by the portal)

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme?.lowercased() == InviteLink.scheme {
            if menuBarController == nil { pendingInviteURL = url } else { handleInvite(url) }
        }
    }

    /// Confirm → `POST /invites/redeem` → key into Keychain `co.wispralt`,
    /// server URL saved, Local mode. The token and key are never logged.
    @MainActor private func handleInvite(_ url: URL) {
        guard let link = InviteLink.parse(url) else {
            Log.warning("Invite link rejected (malformed, or the server address is not allowed).", category: "app")
            return showAlert("This invite link is not valid.", info: LocalURLPolicy.hint)
        }
        guard !refuseInviteForLocalJob() else { return }
        let confirm = NSAlert()
        confirm.messageText = "Join the WisprAlt server?"
        confirm.informativeText = "Server: \(link.host.host ?? "")\n\nThis Mac will dictate through this server "
            + "in Local mode. It replaces the current server URL and API key."
        confirm.addButton(withTitle: "Join")
        confirm.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard confirm.runModal() == .alertFirstButtonReturn else { return }
        // A file may have been picked while the dialog was open.
        guard !refuseInviteForLocalJob() else { return }
        // Until the redeem settles no Local file job may start (see
        // `transcribePickedFile`), so none can be stranded by the new server.
        menuBarController.inviteRedeemInFlight = true
        Task { @MainActor in
            defer { menuBarController.inviteRedeemInFlight = false }
            do {
                let request = link.redeemRequest(deviceLabel: Host.current().localizedName)
                let (data, response) = try await URLSession.shared.data(for: request)
                let redeemed = try InviteLink.decodeRedeem(status: (response as? HTTPURLResponse)?.statusCode ?? 0,
                                                           body: data)
                try KeychainHelper.setAPIKey(redeemed.token)
                Settings.shared.serverURL = link.host
                menuBarController.applyModeSwitch(to: .local)
                Log.info("Invite redeemed; server and key saved.", category: "app")
                menuBarController.showToast(Settings.shared.mode == .local
                    ? "Joined. Local mode is on."
                    : "Joined. Switch to Local once the current upload finishes.")
            } catch {
                Log.warning("invite redeem failed: \(ErrorLog.describe(error))", category: "app")
                showAlert("Could not join the server.",
                          info: (error as? InviteError)?.message ?? "Check your connection and try again.")
            }
        }
    }

    /// Shows why and returns true while a Local file job exists: redeeming
    /// would point that job at the new server and strand its transcript.
    @MainActor private func refuseInviteForLocalJob() -> Bool {
        guard menuBarController.localFileJobActiveOrSaved else { return false }
        Log.info("Invite refused: a Local file job is active or saved.", category: "app")
        showAlert(InviteLink.localJobBusyTitle, info: InviteLink.localJobBusyMessage)
        return true
    }

    @MainActor private func showAlert(_ message: String, info: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = info
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
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
