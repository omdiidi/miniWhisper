import AppKit

/// Root application delegate.
///
/// Responsibilities:
///  - Owns the MenuBarController (strong reference; keeps it alive for the app lifetime).
///  - Owns the SparkleController (strong reference; Sparkle requires long-lived host).
///  - Owns the FNKeyMonitor (strong reference; tap must live for the app lifetime).
///  - On launch: runs PermissionGate.checkAll() asynchronously, then starts FNKeyMonitor.
final class AppDelegate: NSObject, NSApplicationDelegate {
    // MARK: - Owned controllers (strong references)

    private(set) var menuBarController: MenuBarController!
    private var sparkleController: SparkleController!
    /// Retains the CGEvent tap for the entire app lifetime.
    private var fnKeyMonitor: FNKeyMonitor!

    // MARK: - NSApplicationDelegate

    func applicationDidFinishLaunching(_ notification: Notification) {
        Log.info("WisprAlt launching.", category: "app")

        // Instantiate the menubar controller first so the status item is visible immediately.
        menuBarController = MenuBarController()

        // Sparkle auto-update controller (honours meeting-guard gate).
        sparkleController = SparkleController()
        sparkleController.menuBarController = menuBarController

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
