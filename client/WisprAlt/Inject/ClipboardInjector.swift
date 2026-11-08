import AppKit
import CoreGraphics

/// Clipboard delivery. The text is written to the pasteboard and **left
/// there**: a Cmd+V paste cannot be verified, so restoring the old contents
/// could erase the only copy of a dictation that did not land.
enum ClipboardInjector {
    /// Writes `text` to the pasteboard and synthesises Cmd+V into the focused app.
    static func injectViaCmdV(_ text: String) {
        copyOnly(text)
        synthesizeCmdV()
    }

    /// Writes `text` to the pasteboard only.
    static func copyOnly(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    /// Synthesises a Cmd+V key press (key down + key up) directed to the
    /// annotated session event tap (the focused application).
    private static func synthesizeCmdV() {
        // Virtual key 0x09 = 'v' on US ANSI layout. Same value on all Apple keyboards.
        let vKeyCode: CGKeyCode = 0x09

        let keyDown = CGEvent(keyboardEventSource: nil, virtualKey: vKeyCode, keyDown: true)
        keyDown?.flags = .maskCommand
        keyDown?.post(tap: .cgAnnotatedSessionEventTap)

        let keyUp = CGEvent(keyboardEventSource: nil, virtualKey: vKeyCode, keyDown: false)
        keyUp?.flags = .maskCommand
        keyUp?.post(tap: .cgAnnotatedSessionEventTap)
    }
}
