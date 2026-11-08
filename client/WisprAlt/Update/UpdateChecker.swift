import Foundation
import AppKit
import WisprAltCore

/// Lightweight in-app update awareness: polls the GitHub Releases API.
///
/// `checkSoon()` schedules a one-shot check ~60 s after launch, then every
/// 6 h; `check(force:)` debounces to 6 h. When `tag_name` from
/// `/releases/latest` is semver-greater than the bundled
/// `CFBundleShortVersionString`, it sets `Settings.shared.updateAvailable`
/// (Settings > Updates shows "Update available") and badges the menubar icon.
///
/// The repository polled is the `WisprAltReleaseRepo` Info.plist key
/// (`owner/name`), which `scripts/build-client-local.sh` stamps from the
/// clone's GitHub `origin`. Without it (a non-GitHub clone) checks are skipped.
///
/// Installing is the CLI's job: `copyUpdateCommand()` copies
/// `wispralt update --client` (pull, reinstall, rebuild the app) for the user
/// to paste into Terminal.
///
/// All `Settings.shared` and `MenuBarController.shared` mutations happen
/// inside `await MainActor.run { }`.
final class UpdateChecker {

    // MARK: - Singleton

    static let shared = UpdateChecker()

    // MARK: - Constants

    /// What the user runs to update (see `wispralt update -h`).
    static let updateCommand = "wispralt update --client"

    /// `owner/name` of the GitHub repository whose releases are polled.
    private static let releaseRepo: String? = {
        let raw = Bundle.main.infoDictionary?["WisprAltReleaseRepo"] as? String ?? ""
        let parts = raw.split(separator: "/")
        return parts.count == 2 ? raw : nil
    }()

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

        guard let repo = Self.releaseRepo else {
            Log.debug("UpdateChecker: no WisprAltReleaseRepo in the bundle; skipping.", category: "update")
            return
        }
        let bundled = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0.0"

        do {
            let remote = try await fetchLatestTag(repo: repo)
            let newer = Self.isNewer(remote: remote, bundled: bundled)
            Log.info(
                "UpdateChecker: bundled=\(bundled) remote=\(remote) newer=\(newer)",
                category: "update"
            )
            await MainActor.run {
                Settings.shared.lastUpdateCheck = Date()
                Settings.shared.updateAvailable = newer ? remote : nil
                MenuBarController.shared?.setUpdateBadge(visible: newer)
            }
        } catch {
            Log.warning("UpdateChecker: fetchLatestTag failed: \(ErrorLog.describe(error))", category: "update")
            await MainActor.run {
                Settings.shared.lastUpdateCheck = Date()
            }
        }
    }

    // MARK: - Update command

    /// Copy `wispralt update --client` to the clipboard and post
    /// `.updaterCommandCopied` so Settings can say so.
    func copyUpdateCommand() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(Self.updateCommand, forType: .string)
        NotificationCenter.default.post(name: .updaterCommandCopied, object: nil)
    }

    // MARK: - GitHub Releases fetch

    private struct ReleaseResponse: Decodable {
        let tag_name: String
    }

    private func fetchLatestTag(repo: String) async throws -> String {
        let urlString = "https://api.github.com/repos/\(repo)/releases/latest"
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
    /// Posted when `UpdateChecker.copyUpdateCommand()` put the update command
    /// on the pasteboard, so Settings can show a "copied" note.
    static let updaterCommandCopied = Notification.Name("co.wispralt.updaterCommandCopied")
}
