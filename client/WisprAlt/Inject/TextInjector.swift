import AppKit
import ApplicationServices
import Foundation
import WisprAltCore

/// Strategy combinator for text injection.
///
/// Captures focus context once at the top of `inject(_:)` (closing the TOCTOU
/// window between the secure-field check and the AX write), refuses injection
/// outright if the focused element is a native secure field, then tries
/// `AccessibilityInjector` first because it inserts directly at the cursor
/// without touching the clipboard. Falls through to `ClipboardInjector` if the
/// AX strategy reports failure (e.g. focused element is in an Electron app
/// that silently no-ops AX attribute writes — iMessages, Pane, Slack, …).
@MainActor
enum TextInjector {
    /// Injects `text` at the current cursor position in the focused application.
    ///
    /// Strategy order:
    ///   1. If `targetPID` is supplied, activate that app first so focus is
    ///      restored to the window the user was looking at when they finished
    ///      speaking — fixes the "I dictated but text landed in the wrong
    ///      window" failure when the network round-trip is slow enough that
    ///      the user switches apps mid-flight.
    ///   2. Capture `(FocusContext, AXUIElement?)` once.
    ///   3. If `shouldRefuseInjection` is true → log warning, debounced
    ///      notification, return without touching AX or clipboard.
    ///   4. `AccessibilityInjector.tryInsertWith` — AX kAXSelectedTextAttribute
    ///      with read-back verification. Returns true only if the value was
    ///      observed to change.
    ///   5. `ClipboardInjector.injectViaCmdV` — writes text to pasteboard,
    ///      synthesises Cmd+V, then restores the original pasteboard contents
    ///      after 200 ms.
    ///
    /// - Parameters:
    ///   - text: The string to inject (typically transcription output).
    ///   - targetPID: PID of the app that was frontmost when the user finished
    ///     speaking. When non-nil and different from the current frontmost
    ///     (and not WisprAlt itself), the inject path activates that app
    ///     before resolving the AX element, so dictation lands where the user
    ///     intended even if their focus drifted during the upload.
    static func inject(_ text: String, targetPID: pid_t? = nil) async {
        guard !text.isEmpty else {
            Log.debug("TextInjector: empty string — skipping injection.", category: "inject")
            return
        }

        await restoreTargetIfNeeded(targetPID)

        let (context, element) = captureFocus()
        Log.debug("inject: target_at_start=\(context.description)", category: "inject")

        if shouldRefuseInjection(for: context) {
            Log.warning(
                "Dictation skipped: focused element is a secure text field. target=\(context.description)",
                category: "inject"
            )
            notifySecureSkipDebounced(for: context)
            return
        }

        if let element, AccessibilityInjector.tryInsertWith(element: element, text: text) {
            Log.info("Text injected via AX. target=\(context.description)", category: "inject")
            return
        }

        Log.debug("AX injection unverified — using Cmd+V fallback.", category: "inject")
        ClipboardInjector.injectViaCmdV(text)
        Log.info("Text injected via Cmd+V. target=\(context.description)", category: "inject")
    }

    // MARK: - Focus restoration (network-round-trip drift fix)

    /// Activate the app identified by `pid` so subsequent AX/CGEvent calls
    /// hit the user's intended target.
    ///
    /// No-op when:
    ///   - `pid` is nil or non-positive
    ///   - the target is already frontmost (no app switch happened)
    ///   - the target is WisprAlt itself (paranoia — menubar app doesn't
    ///     normally take focus, but defend against the edge case where the
    ///     menubar dropdown caused a brief transition)
    ///   - the target process is no longer running (user quit it)
    ///
    /// After issuing `activate()`, await ~120 ms — `NSRunningApplication.activate`
    /// returns immediately but the frontmost flip happens on the next runloop
    /// tick plus an XPC round-trip to LaunchServices. 120 ms is empirically
    /// enough on Apple Silicon and is dwarfed by the network upload time
    /// that motivated this fix in the first place.
    private static func restoreTargetIfNeeded(_ pid: pid_t?) async {
        guard let pid, pid > 0 else { return }
        let myPID = ProcessInfo.processInfo.processIdentifier
        guard pid != myPID else { return }
        let currentPID = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1
        guard pid != currentPID else { return }
        guard let target = NSRunningApplication(processIdentifier: pid) else {
            Log.debug("inject: target pid=\(pid) no longer running — skipping activation.", category: "inject")
            return
        }
        let activated = target.activate()
        Log.debug(
            "inject: activated target pid=\(pid) bundle=\(target.bundleIdentifier ?? "?") result=\(activated)",
            category: "inject"
        )
        try? await Task.sleep(for: .milliseconds(120))
    }

    // MARK: - Focus capture

    /// Capture the current focused element and a `FocusContext` describing it.
    ///
    /// Returns `(context, nil)` if Accessibility permission is missing or the
    /// system reports no focused element — the caller will then fall through
    /// directly to the clipboard path. All AX calls are bounded by a 250 ms
    /// per-call messaging timeout to cap worst-case stall on hung target apps.
    private static func captureFocus() -> (FocusContext, AXUIElement?) {
        let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1
        let bundleID = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "unknown"

        guard AXIsProcessTrusted() else {
            return (
                FocusContext(
                    bundleID: bundleID, pid: pid,
                    role: "?ax-disabled", subrole: ""
                ),
                nil
            )
        }

        let systemElement = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(systemElement, 0.250)

        var focusedRef: CFTypeRef?
        let focusResult = AXUIElementCopyAttributeValue(
            systemElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        guard focusResult == .success, let focused = focusedRef else {
            return (
                FocusContext(
                    bundleID: bundleID, pid: pid,
                    role: "?no-focus", subrole: ""
                ),
                nil
            )
        }
        // Safe cast: AXUIElementCopyAttributeValue with kAXFocusedUIElementAttribute
        // always returns an AXUIElement when it succeeds. Same pattern as the
        // historical AccessibilityInjector implementation.
        let element = focused as! AXUIElement // swiftlint:disable:this force_cast
        AXUIElementSetMessagingTimeout(element, 0.250)

        var roleRef: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &roleRef)
        let role = (roleRef as? String) ?? "?"

        var subroleRef: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXSubroleAttribute as CFString, &subroleRef)
        let subrole = (subroleRef as? String) ?? ""

        let context = FocusContext(
            bundleID: bundleID, pid: pid,
            role: role, subrole: subrole
        )
        return (context, element)
    }

    // MARK: - Secure-skip notification (60-second debounce)

    private static var lastSecureSkipNotificationAt: Date?
    private static let secureSkipDebounceInterval: TimeInterval = 60

    /// Surface a single user-visible notification when a dictation is refused
    /// because of a secure field — but no more than once per 60 seconds, so a
    /// stuck-on-1Password-unlock scenario doesn't spam Notification Center.
    /// (Logs continue to fire for every event regardless.)
    private static func notifySecureSkipDebounced(for context: FocusContext) {
        let now = Date()
        if let last = lastSecureSkipNotificationAt,
           now.timeIntervalSince(last) < secureSkipDebounceInterval {
            return
        }
        lastSecureSkipNotificationAt = now
        AppNotifications.notify(
            title: "Dictation Skipped",
            body: "\(context.bundleID) is asking for a password. Type the value manually."
        )
    }
}
