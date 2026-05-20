import Foundation
import AppKit

/// v0.5.0 — lightweight in-app update awareness.
///
/// Replaces the disabled Sparkle path with a poll of the GitHub Releases API.
/// `checkSoon()` schedules a one-shot check ~60 s after launch (so we don't
/// race app startup), and `check(force:)` debounces subsequent checks to
/// 6 h.  When `tag_name` from `/releases/latest` is semver-greater than
/// the bundled `CFBundleShortVersionString`, we:
///
///   1. Set `Settings.shared.updateAvailable` to the remote tag (sans
///      leading "v"), which causes the `updateSection` in
///      `SettingsView.advanced` to surface "Update available: vX.Y.Z" plus
///      an "Install now…" button.
///   2. Ask `MenuBarController.shared` to flip its update-badge dot on.
///
/// Triggering the install (`triggerInstall()`) opens Terminal.app and
/// pastes the curl-install one-liner so the existing
/// `scripts/install.sh` path runs unchanged.  If AppleScript fails (TCC
/// prompt declined, sandboxed environment, etc.) we fall back to
/// copying the command to the clipboard and posting
/// `.updaterFallbackToClipboard` so callers can render a toast.
///
/// All `Settings.shared` and `MenuBarController.shared` mutations happen
/// inside `await MainActor.run { }` so the actor isolation rules
/// for `@MainActor`-bound singletons are respected even when `check()`
/// is invoked from a detached background task.
final class UpdateChecker {

    // MARK: - Singleton

    static let shared = UpdateChecker()

    // MARK: - Constants

    private static let repoOwner = "omdiidi"
    private static let repoName = "miniWhisper"
    private static let installURL = "https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh"

    /// 6 hours between automatic polls.  A manual "Check for updates"
    /// press from Settings can override via `check(force: true)`.
    private let debounceInterval: TimeInterval = 6 * 60 * 60

    // MARK: - Init

    private init() {}

    // MARK: - Public scheduling

    /// Schedule the initial 60s-after-launch check AND a recurring 6h loop.
    /// Safe to call from `AppDelegate.applicationDidFinishLaunching` without
    /// delaying launch. The debounce in `check()` also gates the actual fetch,
    /// so a manual "Check for updates" press racing the recurring loop won't
    /// double-fire.
    func checkSoon() {
        // Initial check 60s after launch (avoid races with PermissionGate
        // prompts and the first /me probe).
        Task.detached(priority: .utility) { [weak self] in
            try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
            await self?.check()
        }
        // Recurring 6h loop. Internal debounce in check() guarantees we don't
        // double-fire if a manual "Check for updates" click races with this.
        Task.detached(priority: .utility) { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(6 * 60 * 60) * 1_000_000_000)
                await self?.check()
            }
        }
    }

    /// Perform a check now.  When `force` is true the debounce is
    /// ignored — used by the manual "Check for updates" button in the
    /// Settings → Updates section.
    func check(force: Bool = false) async {
        let lastCheck = await MainActor.run { Settings.shared.lastUpdateCheck }
        if !force,
           let last = lastCheck,
           Date().timeIntervalSince(last) < debounceInterval
        {
            Log.debug(
                "UpdateChecker: skipping check — last poll was \(Int(Date().timeIntervalSince(last)))s ago.",
                category: "update"
            )
            return
        }

        let bundled = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0.0"

        do {
            let remote = try await fetchLatestTag()
            let newer = Self.isNewer(remote: remote, bundled: bundled)
            Log.info(
                "UpdateChecker: bundled=\(bundled) remote=\(remote) newer=\(newer)",
                category: "update"
            )
            await MainActor.run {
                Settings.shared.lastUpdateCheck = Date()
                Settings.shared.updateAvailable = newer ? remote : nil
                // Only badge when a server URL is configured — otherwise
                // the install one-liner won't have a working
                // post-install handoff anyway.
                let badgeOn = newer && Settings.shared.serverURL != nil
                MenuBarController.shared?.setUpdateBadge(visible: badgeOn)
            }
        } catch {
            Log.warning("UpdateChecker: fetchLatestTag failed: \(error)", category: "update")
            await MainActor.run {
                Settings.shared.lastUpdateCheck = Date()
            }
        }
    }

    // MARK: - Install trigger

    /// Open Terminal.app and run the canonical curl-install one-liner.
    /// On AppleScript failure (TCC prompt declined, no Terminal, etc.)
    /// fall back to copying the command to the pasteboard and posting
    /// `.updaterFallbackToClipboard` so a caller can render a toast.
    ///
    /// Refuses to run during an active meeting recording — install.sh kills
    /// the running app, which would lose in-progress meeting audio. Posts
    /// `.updaterMeetingActive` so a caller can show a "finish meeting first"
    /// toast.
    func triggerInstall() {
        if MenuBarController.shared?.isMeetingActive == true {
            Log.warning(
                "UpdateChecker: refusing to install during active meeting.",
                category: "update"
            )
            NotificationCenter.default.post(name: .updaterMeetingActive, object: nil)
            return
        }
        // Hardcoded so a compromised UpdateChecker can't inject a
        // different command: the script source is a literal, no
        // interpolation, no callable.
        let scriptSource = """
        tell application "Terminal"
            activate
            do script "curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash"
        end tell
        """
        var errorInfo: NSDictionary?
        guard let script = NSAppleScript(source: scriptSource) else {
            fallbackToClipboard(reason: "NSAppleScript init returned nil")
            return
        }
        _ = script.executeAndReturnError(&errorInfo)
        if let info = errorInfo {
            let code = (info[NSAppleScript.errorNumber] as? Int) ?? 0
            // -1743: not authorized to send Apple events (TCC declined).
            // -1744: user denied access via TCC prompt.
            // -1728: app not running / can't get reference (Terminal not installed).
            if [-1743, -1744, -1728].contains(code) {
                fallbackToClipboard(reason: "AppleScript error \(code): \(info)")
                return
            }
            Log.warning(
                "UpdateChecker: AppleScript executed with non-fatal error \(code): \(info)",
                category: "update"
            )
        }
    }

    private func fallbackToClipboard(reason: String) {
        Log.warning("UpdateChecker: \(reason) — copying install command to clipboard.", category: "update")
        let command = "curl -fsSL \(Self.installURL) | bash"
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(command, forType: .string)
        NotificationCenter.default.post(name: .updaterFallbackToClipboard, object: nil)
    }

    // MARK: - GitHub Releases fetch

    private struct ReleaseResponse: Decodable {
        let tag_name: String
    }

    private func fetchLatestTag() async throws -> String {
        let urlString = "https://api.github.com/repos/\(Self.repoOwner)/\(Self.repoName)/releases/latest"
        guard let url = URL(string: urlString) else {
            throw UpdateCheckerError.badStatus(0)
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("WisprAlt-UpdateChecker", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode)
        else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw UpdateCheckerError.badStatus(code)
        }
        let decoded = try JSONDecoder().decode(ReleaseResponse.self, from: data)
        var tag = decoded.tag_name
        if tag.hasPrefix("v") {
            tag.removeFirst()
        }
        return tag
    }

    // MARK: - SemVer comparison

    /// Returns true when `remote` is strictly greater than `bundled`.
    /// Both inputs are parsed as dot-separated integer components after
    /// stripping any `+build` suffix.  Missing trailing components are
    /// treated as 0 so "1.0" < "1.0.1".
    static func isNewer(remote: String, bundled: String) -> Bool {
        let r = parts(remote)
        let b = parts(bundled)
        let count = max(r.count, b.count)
        for i in 0..<count {
            let ri = i < r.count ? r[i] : 0
            let bi = i < b.count ? b[i] : 0
            if ri > bi { return true }
            if ri < bi { return false }
        }
        return false
    }

    private static func parts(_ version: String) -> [Int] {
        // Strip "+buildmetadata" if present then split on "."
        let head = version.split(separator: "+", maxSplits: 1).first.map(String.init) ?? version
        return head.split(separator: ".").compactMap { Int($0) }
    }
}

// MARK: - Errors

enum UpdateCheckerError: Error {
    case badStatus(Int)
}

// MARK: - Notification names

extension Notification.Name {
    /// Posted when `UpdateChecker.triggerInstall()` could not launch Terminal
    /// via AppleScript and fell back to copying the install command to the
    /// system pasteboard. Listeners can render a "Command copied — open
    /// Terminal and paste" toast.
    static let updaterFallbackToClipboard = Notification.Name("co.wispralt.updaterFallbackToClipboard")

    /// Posted when `UpdateChecker.triggerInstall()` was invoked while a
    /// meeting recording is active. Install is refused so in-progress audio
    /// isn't lost when install.sh kills the app.
    static let updaterMeetingActive = Notification.Name("co.wispralt.updaterMeetingActive")
}
