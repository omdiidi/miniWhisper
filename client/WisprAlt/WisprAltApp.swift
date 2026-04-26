import SwiftUI

/// Application entry point.
///
/// LSUIElement = true in Info.plist ensures no Dock icon appears.
/// AppDelegate handles all meaningful startup work; the SwiftUI @main struct
/// simply anchors the NSApplication lifecycle and provides the Settings scene
/// (so macOS's "WisprAlt > Settings…" menu item works).
@main
struct WisprAltApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // Empty Settings scene: gives macOS a target for the Settings menu item;
        // the actual settings UI lives in the menubar popover (MenuBarController).
        SwiftUI.Settings {
            EmptyView()
        }
    }
}
