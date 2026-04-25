import Foundation
import UserNotifications

/// App-level notification helpers.
///
/// Call `AppNotifications.requestAuthorization()` once at startup (e.g. in
/// `AppDelegate.applicationDidFinishLaunching`).
/// Call `AppNotifications.notify(title:body:)` from any thread to post a local
/// notification to Notification Center.
enum AppNotifications {
    // MARK: - Authorization

    /// Requests `.alert` + `.sound` authorization from `UNUserNotificationCenter`.
    ///
    /// Safe to call multiple times — UNUserNotificationCenter caches the grant/denial
    /// and does not re-prompt after the first decision. Logs the outcome.
    static func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]
        ) { granted, error in
            if let error {
                Log.error("Notification authorization error: \(error.localizedDescription)", category: "notifications")
                return
            }
            if granted {
                Log.info("Notification permission granted.", category: "notifications")
            } else {
                Log.warning("Notification permission denied by user.", category: "notifications")
            }
        }
    }

    // MARK: - Post notification

    /// Posts a local notification with the given title and body.
    ///
    /// The notification is delivered immediately (no time trigger delay).
    /// If the app is in the foreground, the system may suppress the banner depending
    /// on the app's foreground presentation options — configure those in
    /// `UNUserNotificationCenterDelegate.userNotificationCenter(_:willPresent:)` if
    /// foreground banners are required.
    ///
    /// - Parameters:
    ///   - title: Short notification title (shown in bold).
    ///   - body:  Longer message text.
    static func notify(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil  // nil = deliver immediately
        )

        UNUserNotificationCenter.current().add(request) { error in
            if let error {
                Log.error(
                    "Failed to post notification '\(title)': \(error.localizedDescription)",
                    category: "notifications"
                )
            }
        }
    }
}
