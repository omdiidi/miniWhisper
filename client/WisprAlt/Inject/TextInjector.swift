import Foundation

/// Strategy combinator for text injection.
///
/// Tries `AccessibilityInjector` first because it inserts directly at the cursor
/// without touching the clipboard. Falls through to `ClipboardInjector` if the
/// AX strategy reports failure (e.g. focused element is in an Electron app that
/// silently no-ops AX attribute writes).
enum TextInjector {
    /// Injects `text` at the current cursor position in the focused application.
    ///
    /// Strategy order:
    ///   1. `AccessibilityInjector.tryInsert` — AX kAXSelectedTextAttribute with read-back
    ///      verification. Returns true only if the value was observed to change (or
    ///      best-effort heuristic passed).
    ///   2. `ClipboardInjector.injectViaCmdV` — writes text to pasteboard, synthesises
    ///      Cmd+V, then restores the original pasteboard contents after 200 ms.
    ///
    /// - Parameter text: The string to inject (typically transcription output).
    static func inject(_ text: String) {
        guard !text.isEmpty else {
            Log.debug("TextInjector: empty string — skipping injection.", category: "inject")
            return
        }

        if AccessibilityInjector.tryInsert(text) {
            Log.info("Text injected via AX.", category: "inject")
            return
        }

        Log.debug("AX injection failed — falling through to Cmd+V clipboard strategy.", category: "inject")
        ClipboardInjector.injectViaCmdV(text)
        Log.info("Text injected via Cmd+V.", category: "inject")
    }
}
