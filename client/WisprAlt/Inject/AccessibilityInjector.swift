import ApplicationServices

/// Injects text into the focused accessibility element using the `kAXSelectedTextAttribute`
/// strategy.
///
/// Read-back verification: after setting `kAXSelectedTextAttribute`, we re-read
/// `kAXValueAttribute`. If the value changed we know the injection succeeded.
/// If it did not change (e.g. Electron apps that silently no-op), we return `false`
/// so `TextInjector` can fall through to the clipboard strategy.
///
/// Special case: if the element's value was empty before injection AND the set call
/// returned `.success`, we treat it as success — the app may not expose its value via AX
/// but accepted the insert.
enum AccessibilityInjector {
    /// Attempts to insert `text` at the current cursor position of the focused element.
    ///
    /// - Parameter text: The string to inject.
    /// - Returns: `true` if the text was verified to have been inserted (or best-effort
    ///   heuristic passed); `false` if the element rejected the injection or could not
    ///   be read back to verify.
    @discardableResult
    static func tryInsert(_ text: String) -> Bool {
        // Bail immediately if the app lacks Accessibility permission.
        guard AXIsProcessTrusted() else {
            Log.warning("Accessibility permission not granted — cannot inject via AX.", category: "inject")
            return false
        }

        // Find the system-wide focused element.
        let systemElement = AXUIElementCreateSystemWide()
        var focusedRef: CFTypeRef?
        let focusResult = AXUIElementCopyAttributeValue(
            systemElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        guard focusResult == .success, let focused = focusedRef else {
            Log.debug("No focused AX element found.", category: "inject")
            return false
        }
        // Safe cast: AXUIElementCopyAttributeValue with kAXFocusedUIElementAttribute always
        // returns an AXUIElement when it succeeds.
        let element = focused as! AXUIElement // swiftlint:disable:this force_cast

        // Snapshot the current value so we can detect whether injection changed it.
        var beforeRef: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &beforeRef)
        let beforeValue = (beforeRef as? String) ?? ""

        // Attempt to inject via kAXSelectedTextAttribute (replaces current selection).
        let setResult = AXUIElementSetAttributeValue(
            element,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        )
        guard setResult == .success else {
            Log.debug("AXUIElementSetAttributeValue returned \(setResult.rawValue).", category: "inject")
            return false
        }

        // Read-back verification: confirm the value actually changed.
        var afterRef: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &afterRef)
        let afterValue = (afterRef as? String) ?? ""

        if afterValue != beforeValue {
            // Value changed — injection verified.
            return true
        }

        // Value unchanged. If the element's value was empty before, apply best-effort
        // heuristic: the set returned success, so accept it (app may not expose value via AX).
        if beforeValue.isEmpty {
            Log.debug("AX injection: value was empty before and set succeeded — accepting.", category: "inject")
            return true
        }

        // Value unchanged and was non-empty before — silent no-op (Electron pattern).
        Log.debug("AX injection produced no visible change — falling through to clipboard.", category: "inject")
        return false
    }
}
