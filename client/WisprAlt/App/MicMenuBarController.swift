import AppKit
import AVFoundation

final class MicMenuBarController: NSObject {
    private let statusItem: NSStatusItem
    private let menu = NSMenu()

    weak var menuBarController: MenuBarController?

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()
        configureStatusItem()
        menu.delegate = self
        statusItem.menu = menu  // left-click → menu pops natively
    }

    private func configureStatusItem() {
        guard let button = statusItem.button else { return }
        let img = NSImage(systemSymbolName: "mic", accessibilityDescription: "Input Mic")
        img?.isTemplate = true
        button.image = img
        button.toolTip = "Input mic"
    }

    /// KEEP isTemplate=true — contentTintColor only works on templates.
    func updateRecordingTint(active: Bool) {
        guard let button = statusItem.button else { return }
        // Image stays template; tint switches between nil (system) and red.
        button.image?.isTemplate = true
        button.contentTintColor = active ? .systemRed : nil
    }
}

extension MicMenuBarController: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let devices = MicEnumerator.availableInputs()
        let preferred = Settings.shared.preferredInputDeviceUID

        // Header (disabled).
        let header = NSMenuItem(title: "Input Mic", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        // System default option.
        let sysName = MicEnumerator.systemDefaultInputName()
        let sysItem = NSMenuItem(
            title: "System Default" + (sysName.map { " (\($0))" } ?? ""),
            action: #selector(selectSystemDefault),
            keyEquivalent: ""
        )
        sysItem.target = self
        if preferred == nil { sysItem.state = .on }
        menu.addItem(sysItem)
        menu.addItem(.separator())

        // Permission-revoked / empty fallback.
        if devices.isEmpty {
            let warn = NSMenuItem(
                title: "No input devices found — check Microphone permission",
                action: #selector(openMicPrivacy),
                keyEquivalent: ""
            )
            warn.target = self
            menu.addItem(warn)
        } else {
            for d in devices {
                let item = NSMenuItem(
                    title: d.name,
                    action: #selector(selectDevice(_:)),
                    keyEquivalent: ""
                )
                item.target = self
                item.representedObject = d.uniqueID
                if d.uniqueID == preferred { item.state = .on }
                menu.addItem(item)
            }
        }
        menu.addItem(.separator())

        // Footer.
        let openItem = NSMenuItem(
            title: "Open Sound Settings…",
            action: #selector(openSoundSettings),
            keyEquivalent: ""
        )
        openItem.target = self
        menu.addItem(openItem)
    }

    @objc private func selectSystemDefault() {
        Settings.shared.preferredInputDeviceUID = nil
        announceSwitch(to: "System default")
    }

    @objc private func selectDevice(_ sender: NSMenuItem) {
        guard let uid = sender.representedObject as? String else { return }
        Settings.shared.preferredInputDeviceUID = uid
        announceSwitch(to: sender.title)
    }

    @objc private func openSoundSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.Sound-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func openMicPrivacy() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    private func announceSwitch(to label: String) {
        let isRecording = (menuBarController?.mode == .meetingRecording)
            || (menuBarController?.mode == .dictating)
        if isRecording {
            AppNotifications.notify(
                title: "Mic switched to \(label)",
                body: "Active recording is unchanged. Next recording will use this mic."
            )
        } else {
            Log.info("Preferred input mic set to: \(label)", category: "audio")
        }
    }
}
