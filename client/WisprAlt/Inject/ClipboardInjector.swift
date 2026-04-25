import AppKit
import CoreGraphics

/// Injects text by writing it to the pasteboard and synthesising a Cmd+V keystroke.
///
/// Rich-clipboard preservation (Maccy-style):
///   1. Snapshot all existing pasteboard items, storing raw `Data` per type, skipping
///      `dyn.*` type UTIs (private internal types that cannot be round-tripped).
///   2. Save the current `changeCount`.
///   3. Write the new string to the pasteboard.
///   4. Synthesise keyDown + keyUp for virtual key 0x09 (`v`) with `.maskCommand`.
///   5. After 200 ms, if `pb.changeCount == saved + 1` (only our paste incremented it,
///      meaning no other process wrote to the clipboard between our write and the restore),
///      restore the original items.
enum ClipboardInjector {
    // MARK: - Public interface

    /// Writes `text` to the system pasteboard and synthesises Cmd+V into the focused app.
    ///
    /// - Parameter text: The string to inject.
    static func injectViaCmdV(_ text: String) {
        let pasteboard = NSPasteboard.general

        // --- 1. Snapshot existing pasteboard contents ---
        let savedItems = snapshotPasteboard(pasteboard)
        let savedChangeCount = pasteboard.changeCount

        // --- 2. Write new string ---
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)

        // --- 3. Synthesise Cmd+V ---
        synthesizeCmdV()

        // --- 4. Restore after 200 ms if changeCount is exactly ours ---
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.20) {
            // Only restore if the changeCount moved by exactly 1 (our paste write).
            // A count of saved+2 or more means another process also wrote to the clipboard
            // in the interim — don't stomp their content.
            guard pasteboard.changeCount == savedChangeCount + 1 else {
                Log.debug(
                    "Clipboard changeCount advanced by \(pasteboard.changeCount - savedChangeCount); skipping restore.",
                    category: "inject"
                )
                return
            }
            restorePasteboard(pasteboard, items: savedItems)
        }
    }

    // MARK: - Private helpers

    /// Returns a snapshot of all current pasteboard items as `SavedItem` records.
    /// Types whose UTI begins with `dyn.` are skipped because they represent private
    /// app-internal types that cannot be reconstituted from raw data.
    private static func snapshotPasteboard(_ pb: NSPasteboard) -> [SavedItem] {
        guard let items = pb.pasteboardItems else { return [] }
        return items.compactMap { item -> SavedItem? in
            var typeDataPairs: [(NSPasteboard.PasteboardType, Data)] = []
            for type_ in item.types {
                // Skip dynamic UTI types (dyn.*) — they are private and non-transferable.
                guard !type_.rawValue.hasPrefix("dyn.") else { continue }
                if let data = item.data(forType: type_) {
                    typeDataPairs.append((type_, data))
                }
            }
            guard !typeDataPairs.isEmpty else { return nil }
            return SavedItem(typeDataPairs: typeDataPairs)
        }
    }

    /// Writes `items` back to `pb`, clearing existing contents first.
    private static func restorePasteboard(_ pb: NSPasteboard, items: [SavedItem]) {
        guard !items.isEmpty else { return }
        pb.clearContents()
        let pbItems = items.map { saved -> NSPasteboardItem in
            let item = NSPasteboardItem()
            for (type_, data) in saved.typeDataPairs {
                item.setData(data, forType: type_)
            }
            return item
        }
        pb.writeObjects(pbItems)
        Log.debug("Clipboard restored to pre-injection state.", category: "inject")
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

    // MARK: - Snapshot type

    /// A snapshot of a single `NSPasteboardItem`'s typed data.
    private struct SavedItem {
        let typeDataPairs: [(NSPasteboard.PasteboardType, Data)]
    }
}
