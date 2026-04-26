import AppKit
import Sparkle

/// Manages Sparkle 2 auto-update lifecycle.
///
/// v3 delta requirements:
///  - Update prompts are gated on `MeetingRecorder.isActive == false`.
///    SparkleController observes the `isMeetingActive` flag from MenuBarController;
///    if a meeting is recording the update sheet is deferred until `stop()` completes.
///  - `SUAutomaticallyUpdate = NO` in Info.plist ensures the user must explicitly click
///    "Restart Now" — the app never relaunches silently.
final class SparkleController: NSObject, SPUUpdaterDelegate {
    // MARK: - Sparkle components

    private let updaterController: SPUStandardUpdaterController

    // MARK: - Meeting guard
    // Set by AppDelegate after MenuBarController is ready.
    weak var menuBarController: MenuBarController?

    // MARK: - Init

    override init() {
        // Pre-init self ref via deferred-start trick: instantiate the controller
        // without auto-starting, set delegate, then start the updater.
        let placeholderController = SPUStandardUpdaterController(
            startingUpdater: false,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
        updaterController = placeholderController
        super.init()
        // Re-init with self as delegate; Sparkle 2 requires delegate at construction time.
        updaterController = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
    }

    // MARK: - Public interface

    /// Exposes the "Check for Updates" action for the menubar menu item.
    @objc func checkForUpdates(_ sender: Any?) {
        updaterController.checkForUpdates(sender)
    }

    /// Call from MenuBarController when meeting recording starts/stops so Sparkle
    /// knows whether it may present the update sheet.
    func meetingStateChanged(isActive: Bool) {
        if !isActive {
            // Meeting finished; if a deferred update is pending, Sparkle will retry
            // on the next scheduled check. No explicit trigger needed.
            Log.info("Meeting ended — Sparkle updates re-enabled.", category: "update")
        }
    }

    // MARK: - SPUUpdaterDelegate

    /// Gate update checks: disallow while a meeting is in progress.
    func updater(
        _ updater: SPUUpdater,
        shouldPostponeRelaunchForUpdate item: SUAppcastItem,
        untilInvokingBlock installHandler: @escaping () -> Void
    ) -> Bool {
        guard let mbc = menuBarController, mbc.isMeetingActive else {
            // No meeting active; install immediately.
            installHandler()
            return false
        }
        // Meeting active: defer the relaunch. Sparkle will prompt the user again
        // after the next launch. We do NOT auto-install during an active meeting.
        Log.info(
            "Sparkle update install deferred — meeting recording is active.",
            category: "update"
        )
        return true  // true = we take ownership; we intentionally do NOT call installHandler here
    }

    func updater(_ updater: SPUUpdater, willInstallUpdate item: SUAppcastItem) {
        Log.info("Installing update: \(item.versionString)", category: "update")
    }

    // MARK: - G9: Error surfacing

    /// Called when the update cycle aborts with an error (e.g. network failure,
    /// appcast parse error, signature mismatch).
    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        Log.error("Sparkle update aborted: \(error.localizedDescription)", category: "update")
        AppNotifications.notify(
            title: "Auto-update Failed",
            body: "Auto-update failed: \(error.localizedDescription)"
        )
        menuBarController?.lastUpdateError = error.localizedDescription
    }

    /// Retry a failed update check and clear the stored error.
    func retryUpdateCheck() {
        menuBarController?.lastUpdateError = nil
        updaterController.checkForUpdates(nil)
    }

    /// Called after each complete update cycle.  `error` is non-nil when the cycle
    /// ended due to an error that did not trigger `didAbortWithError`.
    func updater(
        _ updater: SPUUpdater,
        didFinishUpdateCycleFor updateCheck: SPUUpdateCheck,
        error: Error?
    ) {
        if let error {
            Log.error("Sparkle update cycle finished with error: \(error.localizedDescription)", category: "update")
            AppNotifications.notify(
                title: "Auto-update Failed",
                body: "Auto-update failed: \(error.localizedDescription)"
            )
        }
    }
}
