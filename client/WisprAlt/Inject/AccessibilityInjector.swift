import ApplicationServices
import WisprAltCore

/// Injects text into a pre-captured focused accessibility element using the
/// `kAXSelectedTextAttribute` strategy.
///
/// Read-back verification: after setting `kAXSelectedTextAttribute`, we re-read
/// `kAXValueAttribute`. We only return `true` if the value is observed to change.
/// If the read fails or the value is unchanged (Electron, iMessages, and other apps
/// that don't expose their value via AX or silently no-op the write), we return `false`
/// so `TextInjector` falls through to the clipboard strategy.
///
/// The caller (`TextInjector`) is responsible for capturing the focused element
/// and the surrounding `FocusContext`. Passing the element in (rather than
/// re-walking system-wide focus here) closes the TOCTOU window between the
/// security check and the AX write.
enum AccessibilityInjector {
    /// Insert `text` into a specific, pre-captured focused element.
    ///
    /// - Parameters:
    ///   - element: The focused `AXUIElement` captured by the caller.
    ///   - text: The string to inject.
    /// - Returns: `true` only if a read-back of `kAXValueAttribute` confirms the value
    ///   changed; `false` otherwise. A `false` return triggers the clipboard fallback.
    @discardableResult
    static func tryInsertWith(element: AXUIElement, text: String) -> Bool {
        // Bound stalling on hung target apps to 250 ms per AX call.
        AXUIElementSetMessagingTimeout(element, 0.250)

        // Snapshot the current value so we can detect whether injection changed it.
        var beforeRef: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &beforeRef)

        // Attempt to inject via kAXSelectedTextAttribute (replaces current selection).
        let setResult = AXUIElementSetAttributeValue(
            element,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        )

        // Read-back verification: confirm the value actually changed.
        var afterRef: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &afterRef)

        let landed = didInjectionLand(
            setSucceeded: setResult == .success,
            beforeValue: beforeRef as? String,
            afterValue: afterRef as? String
        )

        if !landed {
            Log.debug(
                "AX injection unverified (set=\(setResult.rawValue)) — falling through.",
                category: "inject"
            )
        }
        return landed
    }
}
