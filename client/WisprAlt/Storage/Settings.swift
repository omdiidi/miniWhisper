import Foundation
import Combine
import ServiceManagement

/// Persistent user preferences backed by UserDefaults.
///
/// IMPORTANT: The API key is never stored here. It lives exclusively in the Keychain
/// via KeychainHelper. See plan v3 delta "API key NOT here (Keychain only)."
final class Settings: ObservableObject {
    // MARK: - Singleton / shared instance

    static let shared = Settings()

    // MARK: - Private backing store

    private let defaults: UserDefaults

    /// UserDefaults suite matching the app bundle ID.
    private static let suiteName = "co.wispralt.WisprAlt"

    // MARK: - UserDefaults keys

    private enum Key {
        static let serverURL = "serverURL"
        static let meetingsPath = "meetingsPath"
        static let holdMinDuration = "holdMinDuration"
        static let tripleTapWindow = "tripleTapWindow"
        static let maxMeetingMinutes = "maxMeetingMinutes"
        static let preferredInputDeviceUID = "preferredInputDeviceUID"
        static let showMicStatusItem = "showMicStatusItem"
    }

    // MARK: - Published properties

    /// The transcription server base URL. Must use https scheme.
    /// Set to nil to clear. Validated on assignment.
    @Published var serverURL: URL? {
        didSet {
            if let url = serverURL {
                guard url.scheme == "https" else {
                    Log.warning(
                        "serverURL rejected — scheme must be https: \(url.absoluteString)",
                        category: "settings"
                    )
                    // Revert to previous persisted value without triggering another didSet.
                    serverURL = loadServerURL()
                    return
                }
                defaults.set(url.absoluteString, forKey: Key.serverURL)
            } else {
                defaults.removeObject(forKey: Key.serverURL)
            }
        }
    }

    /// Local directory where completed meeting transcripts are saved.
    /// Defaults to ~/Documents/WisprAlt/Meetings.
    @Published var meetingsPath: URL {
        didSet {
            defaults.set(meetingsPath.path, forKey: Key.meetingsPath)
        }
    }

    /// Minimum FN-hold duration (seconds) before dictation starts. Default 0.30.
    /// Clamped to [0.10, 1.00] on set.
    @Published var holdMinDuration: Double {
        didSet {
            let clamped = min(max(holdMinDuration, 0.10), 1.00)
            if clamped != holdMinDuration {
                holdMinDuration = clamped  // triggers didSet once more, but value is already clamped
                return
            }
            defaults.set(holdMinDuration, forKey: Key.holdMinDuration)
        }
    }

    /// Window (seconds) within which three FN taps must occur to trigger meeting recording. Default 0.40.
    /// Clamped to [0.20, 0.80] on set.
    @Published var tripleTapWindow: Double {
        didSet {
            let clamped = min(max(tripleTapWindow, 0.20), 0.80)
            if clamped != tripleTapWindow {
                tripleTapWindow = clamped
                return
            }
            defaults.set(tripleTapWindow, forKey: Key.tripleTapWindow)
        }
    }

    /// Maximum meeting recording duration in minutes. Default 90.
    /// Clamped to [5, 240] on set.
    @Published var maxMeetingMinutes: Int {
        didSet {
            let clamped = min(max(maxMeetingMinutes, 5), 240)
            if clamped != maxMeetingMinutes {
                maxMeetingMinutes = clamped
                return
            }
            defaults.set(maxMeetingMinutes, forKey: Key.maxMeetingMinutes)
        }
    }

    @Published var preferredInputDeviceUID: String? {
        didSet {
            if let uid = preferredInputDeviceUID {
                defaults.set(uid, forKey: Key.preferredInputDeviceUID)
            } else {
                defaults.removeObject(forKey: Key.preferredInputDeviceUID)
            }
        }
    }

    @Published var showMicStatusItem: Bool {
        didSet { defaults.set(showMicStatusItem, forKey: Key.showMicStatusItem) }
    }

    // MARK: - Init

    private init() {
        let suite = UserDefaults(suiteName: Settings.suiteName) ?? .standard
        self.defaults = suite

        // Load persisted values; fall back to defaults.
        let storedMeetingsPath = suite.string(forKey: Key.meetingsPath).flatMap {
            URL(fileURLWithPath: $0, isDirectory: true)
        } ?? Settings.defaultMeetingsPath()

        let storedHold = suite.object(forKey: Key.holdMinDuration) as? Double ?? 0.30
        let storedTriple = suite.object(forKey: Key.tripleTapWindow) as? Double ?? 0.40
        let storedMaxMeetingMinutes = suite.object(forKey: Key.maxMeetingMinutes) as? Int ?? 90
        let storedPreferredInputUID = suite.string(forKey: Key.preferredInputDeviceUID)
        let storedShowMicStatusItem = suite.object(forKey: Key.showMicStatusItem) as? Bool ?? true

        // @Published properties must be set before the object is fully initialised;
        // assign directly via stored property (bypasses didSet observers).
        self._meetingsPath = Published(initialValue: storedMeetingsPath)
        self._holdMinDuration = Published(initialValue: storedHold)
        self._tripleTapWindow = Published(initialValue: storedTriple)
        self._maxMeetingMinutes = Published(initialValue: storedMaxMeetingMinutes)
        self._preferredInputDeviceUID = Published(initialValue: storedPreferredInputUID)
        self._showMicStatusItem = Published(initialValue: storedShowMicStatusItem)
        self._serverURL = Published(initialValue: nil) // set below after init completes
        self.serverURL = loadServerURL(from: suite)
    }

    // MARK: - Helpers

    private func loadServerURL() -> URL? {
        loadServerURL(from: defaults)
    }

    private func loadServerURL(from store: UserDefaults) -> URL? {
        guard let raw = store.string(forKey: Key.serverURL),
              let url = URL(string: raw),
              url.scheme == "https"
        else { return nil }
        return url
    }

    private static func defaultMeetingsPath() -> URL {
        let docs = FileManager.default.urls(
            for: .documentDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Documents")
        return docs
            .appendingPathComponent("WisprAlt", isDirectory: true)
            .appendingPathComponent("Meetings", isDirectory: true)
    }
}

// MARK: - Launch-at-login (SMAppService)

/// @MainActor isolation ensures `objectWillChange.send()` always fires on the
/// main thread even if some future caller mutates `launchAtLogin` from a
/// background Task.
@MainActor
extension Settings {

    /// Whether WisprAlt is registered to launch automatically after login.
    ///
    /// Reads the live `SMAppService.mainApp.status`; writing calls `register()`
    /// or `unregister()` and fires `objectWillChange` so any observing SwiftUI
    /// views update immediately.
    ///
    /// Requires the app to be signed with an Apple Development (or stronger)
    /// code-signing identity. Ad-hoc or self-signed identities cause
    /// `register()` to silently fail at runtime (Apple Developer Forums #799910).
    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue {
                    try SMAppService.mainApp.register()
                } else {
                    try SMAppService.mainApp.unregister()
                }
                objectWillChange.send()
            } catch {
                Log.warning("Launch-at-login toggle failed: \(error)", category: "lifecycle")
            }
        }
    }
}
