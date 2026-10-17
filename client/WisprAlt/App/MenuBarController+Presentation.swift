import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Status item, popover, icon and windows

extension MenuBarController {
    /// Subscribe to `FirstLaunchCoordinator.shared.$isPresentingNameSheet` so the
    /// standalone NSWindow shows/hides in lockstep with the coordinator's state.
    ///
    /// `FirstLaunchCoordinator` is `@MainActor`-isolated, so we hop onto the main
    /// actor to subscribe. Hop is one-shot at init; subsequent sink fires already
    /// run on main since the publisher's source mutations are main-isolated.
    func configureFirstLaunchObserver() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            FirstLaunchCoordinator.shared.$isPresentingNameSheet
                .removeDuplicates()
                .sink { [weak self] isPresented in
                    if isPresented {
                        self?.presentFirstLaunchNameWindow()
                    } else {
                        self?.firstLaunchWindow?.close()
                    }
                }
                .store(in: &self.cancellables)
        }
    }

    /// Display the first-launch name sheet as a standalone window.
    /// Avoids NSPopover + SwiftUI .sheet incompatibility on macOS 15.
    func presentFirstLaunchNameWindow() {
        if firstLaunchWindow == nil {
            let host = NSHostingController(
                rootView: DisplayNameSheet()
                    .environmentObject(FirstLaunchCoordinator.shared)
            )
            let win = NSWindow(contentViewController: host)
            win.title = "Welcome to WisprAlt"
            win.styleMask = [.titled, .closable]
            win.isReleasedWhenClosed = false
            win.delegate = self  // sync window-close back to coordinator state
            win.center()
            win.level = .floating  // keep above other windows
            firstLaunchWindow = win
        }
        firstLaunchWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func configureStatusItem() {
        if let button = statusItem.button {
            button.action = #selector(handleStatusItemClick(_:))
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    /// Build a fresh hosting controller for the popover's root view. Called
    /// every time the popover is shown so SwiftUI state (scroll position,
    /// expanded sections, focus) resets to its default. The controller is
    /// released in `popoverDidClose` so only one ever lives at a time.
    func makePopoverRootViewController() -> NSViewController {
        NSHostingController(
            rootView: SettingsView()
                .environmentObject(Settings.shared)
                .environmentObject(recordingState)
        )
    }

    func configurePopover() {
        popover.behavior = .transient
        popover.delegate = self
        // Belt-and-suspenders: NSPopover.behavior = .transient is supposed to
        // auto-close on click-outside, but in LSUIElement (menubar-only) apps
        // the popover sometimes stays open when the user clicks into another
        // app because the WisprAlt process never had key focus to lose. Force
        // close on any app-deactivation event. Token is stored on the controller
        // so deinit can remove it (matters if MenuBarController is ever recreated).
        didResignActiveObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            if self.popover.isShown {
                self.popover.performClose(nil)
            }
        }
    }

    // MARK: - Icon update

    func updateIcon() {
        guard let button = statusItem.button else { return }

        switch mode {
        case .meetingRecording:
            let composite = renderRecComposite()
            button.image = composite
            button.contentTintColor = nil
            button.attributedTitle = NSAttributedString(string: "")
            button.title = ""
            button.imagePosition = .imageOnly
            button.toolTip = "WisprAlt — Meeting Recording"

        default:
            let (symbolName, accessibilityLabel): (String, String) = {
                switch mode {
                case .idle:             return ("mic", "WisprAlt — Idle")
                case .dictating:        return ("mic.fill", "WisprAlt — Dictating")
                case .converting:       return ("arrow.triangle.2.circlepath", "WisprAlt — Converting…")
                case .uploading:        return ("icloud.and.arrow.up", "WisprAlt — Uploading")
                case .processing:       return ("waveform", "WisprAlt — Processing")
                case .done:             return ("checkmark.circle", "WisprAlt — Done")
                case .meetingRecording: return ("mic", "WisprAlt")  // unreachable
                }
            }()
            let baseImage = NSImage(
                systemSymbolName: symbolName,
                accessibilityDescription: accessibilityLabel
            )
            baseImage?.isTemplate = true
            button.image = updateBadgeVisible
                ? Self.compositeDot(on: baseImage)
                : baseImage
            button.contentTintColor = nil
            button.attributedTitle = NSAttributedString(string: "")
            button.title = ""
            button.imagePosition = .imageOnly
            button.toolTip = accessibilityLabel
        }
    }

    /// Compose an SF Symbol with an orange dot in the upper-right corner.
    /// Returns nil if `base` is nil. Manually tints the SF Symbol with
    /// `NSColor.controlTextColor` so the composite reads correctly on both
    /// light and dark menubars (the composite's `isTemplate=false` disables
    /// the automatic template-image tinting).
    static func compositeDot(on base: NSImage?) -> NSImage? {
        guard let base else { return nil }
        let dim = max(NSStatusBar.system.thickness, 22)
        let size = NSSize(width: dim, height: dim)
        let composite = NSImage(size: size)
        composite.lockFocus()
        if let tintedBase = base.copy() as? NSImage {
            tintedBase.lockFocus()
            NSColor.controlTextColor.set()
            NSRect(origin: .zero, size: tintedBase.size).fill(using: .sourceAtop)
            tintedBase.unlockFocus()
            tintedBase.isTemplate = false
            tintedBase.draw(
                in: NSRect(origin: .zero, size: size),
                from: .zero,
                operation: .sourceOver,
                fraction: 1.0
            )
        } else {
            base.draw(in: NSRect(origin: .zero, size: size))
        }
        let dotSide = dim * 0.30
        let dotRect = NSRect(
            x: dim - dotSide - 1,
            y: dim - dotSide - 1,
            width: dotSide,
            height: dotSide
        )
        NSColor.white.setFill()
        NSBezierPath(ovalIn: dotRect.insetBy(dx: -1, dy: -1)).fill()
        NSColor.systemOrange.setFill()
        NSBezierPath(ovalIn: dotRect).fill()
        composite.unlockFocus()
        composite.isTemplate = false
        return composite
    }

    // MARK: - Popover toggle

    @objc func handleStatusItemClick(_ sender: NSStatusBarButton) {
        if popover.isShown {
            popover.performClose(sender)
        } else if let button = statusItem.button {
            // Build the hosting controller on open (released on close) so
            // SwiftUI state (scroll position, expanded sections, focus)
            // starts from its initial values on every open.
            popover.contentViewController = makePopoverRootViewController()
            popover.show(
                relativeTo: button.bounds,
                of: button,
                preferredEdge: .minY
            )
            // Bring app to front so the popover keyboard-focuses correctly.
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    // MARK: - Composite REC icon

    func renderRecComposite() -> NSImage {
        let dotSize: CGFloat = 8
        let dotGap: CGFloat = 3
        let verticalPadding: CGFloat = 1  // descender clearance
        let font = NSFont.systemFont(ofSize: 11, weight: .bold)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.systemRed,
        ]
        let text = NSAttributedString(string: "REC", attributes: attrs)
        let textSize = text.size()
        let canvasHeight = ceil(textSize.height) + verticalPadding * 2
        let canvasWidth = dotSize + dotGap + ceil(textSize.width) + 2
        let img = NSImage(
            size: NSSize(width: canvasWidth, height: canvasHeight),
            flipped: false
        ) { _ in
            let rect = NSRect(x: 0, y: 0, width: canvasWidth, height: canvasHeight)
            let dotRect = NSRect(
                x: 0,
                y: (rect.height - dotSize) / 2,
                width: dotSize,
                height: dotSize
            )
            NSColor.systemRed.setFill()
            NSBezierPath(ovalIn: dotRect).fill()
            text.draw(in: NSRect(
                x: dotSize + dotGap,
                y: verticalPadding,
                width: ceil(textSize.width),
                height: ceil(textSize.height)
            ))
            return true
        }
        img.isTemplate = false  // pre-rendered red, not a template
        return img
    }
}

// MARK: - NSWindowDelegate (first-launch name sheet)

extension MenuBarController: NSWindowDelegate {
    /// When the user closes the first-launch name window via the title-bar close
    /// button, sync that intent back to the coordinator (treat as Skip).
    ///
    /// `windowWillClose` ALSO fires on programmatic close — when the coordinator's
    /// `recordSave()` or `recordSkip()` flips `isPresentingNameSheet` to false and
    /// the Combine sink calls `window.close()`. In that case, the coordinator
    /// already updated its state and we must NOT call `recordSkip()` again — that
    /// would clobber the success path (overwriting the cleared skip flag with a
    /// fresh 30-day suppression and re-classifying a successful save as a skip).
    ///
    /// Distinguish: if `isPresentingNameSheet == true` at close time, it's a
    /// user-initiated title-bar dismiss. If already `false`, recordSkip/recordSave
    /// already ran — leave coordinator state alone.
    nonisolated func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow else { return }
        Task { @MainActor [weak self] in
            guard let self, closing === self.firstLaunchWindow else { return }
            if FirstLaunchCoordinator.shared.isPresentingNameSheet {
                FirstLaunchCoordinator.shared.recordSkip()
            }
        }
    }
}

// MARK: - NSPopoverDelegate (release SettingsView host on close)

extension MenuBarController: NSPopoverDelegate {
    /// Drop the hosting controller when the popover closes so the SwiftUI
    /// hierarchy is freed instead of lingering until the next open replaces it.
    func popoverDidClose(_ notification: Notification) {
        popover.contentViewController = nil
    }
}
