import Foundation
import SwiftUI

/// Drives the first-launch "What should we call you?" dialog.
///
/// `MenuBarController` is an `NSObject`, NOT `ObservableObject` — `@Published`
/// on it does not trigger SwiftUI updates. This dedicated coordinator owns the
/// `isPresentingNameSheet` state and the 30-day skip suppression so the dialog
/// doesn't nag on every cold launch.
@MainActor
final class FirstLaunchCoordinator: ObservableObject {
    static let shared = FirstLaunchCoordinator()

    @Published var isPresentingNameSheet: Bool = false

    private let suite = UserDefaults(suiteName: "co.wispralt.WisprAlt") ?? .standard
    private let lastSkippedKey = "displayName.lastSkippedAt"

    /// Call after a successful GET /me. Presents the sheet only when
    /// (a) display_name is null on the server AND
    /// (b) the user hasn't skipped within the last 30 days.
    func maybePresentNameSheet(serverDisplayName: String?) {
        guard serverDisplayName == nil else { return }
        if let last = suite.object(forKey: lastSkippedKey) as? Date {
            let thirtyDays: TimeInterval = 30 * 24 * 60 * 60
            if Date().timeIntervalSince(last) < thirtyDays { return }
        }
        isPresentingNameSheet = true
    }

    /// Called when user taps "Skip" — suppress for 30 days.
    func recordSkip() {
        suite.set(Date(), forKey: lastSkippedKey)
        isPresentingNameSheet = false
    }

    /// Called when user successfully saves a name — clear the skip flag.
    func recordSave() {
        suite.removeObject(forKey: lastSkippedKey)
        isPresentingNameSheet = false
    }
}
