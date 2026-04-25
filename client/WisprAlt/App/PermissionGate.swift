import AppKit
import AVFoundation
import CoreGraphics
import ScreenCaptureKit

// MARK: - PermissionStatus

/// Result type returned for each step of the permission wizard.
enum PermissionStatus {
    case granted
    case denied
    case unknown
}

// MARK: - PermissionGate

/// Sequential 4-permission wizard executed at first launch.
///
/// Order (mandated by plan):
///   1. Accessibility
///   2. Input Monitoring
///   3. Microphone
///   4. Screen Recording
///
/// Each step shows an NSAlert with a "Open System Settings" deep-link button when
/// the permission is not yet granted. For Input Monitoring on macOS 14.4+, after
/// the TCC prompt is triggered the app must quit and reopen; a blocking sheet enforces this.
enum PermissionGate {
    // MARK: - System Settings deep-link URLs

    private static let accessibilityURL =
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    private static let inputMonitoringURL =
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
    private static let microphoneURL =
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
    private static let screenRecordingURL =
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"

    // MARK: - Public entry point

    /// Run the sequential 4-step permission check. Must be called on @MainActor.
    /// Returns a status for each permission in order: [accessibility, inputMonitoring, microphone, screenRecording].
    @MainActor
    static func checkAll() async -> [PermissionStatus] {
        var results: [PermissionStatus] = []

        let a = await checkAccessibility()
        results.append(a)

        let im = await checkInputMonitoring()
        results.append(im)

        let mic = await checkMicrophone()
        results.append(mic)

        let sr = await checkScreenRecording()
        results.append(sr)

        return results
    }

    // MARK: - Step 1: Accessibility

    @MainActor
    private static func checkAccessibility() async -> PermissionStatus {
        if AXIsProcessTrusted() {
            Log.info("Accessibility: already trusted.", category: "permissions")
            return .granted
        }

        Log.info("Accessibility: requesting trust.", category: "permissions")
        // Triggers the TCC prompt (shows "Allow" dialog if not yet decided).
        let opts = [kAXTrustedCheckOptionPrompt: true] as CFDictionary
        let trusted = AXIsProcessTrustedWithOptions(opts)

        if trusted {
            return .granted
        }

        // Not trusted — prompt failed or was declined; show alert.
        showSettingsAlert(
            title: "Accessibility Access Required",
            message: "WisprAlt needs Accessibility permission to insert transcribed text at your cursor. Please enable it in System Settings, then relaunch the app.",
            settingsURL: accessibilityURL
        )
        return .denied
    }

    // MARK: - Step 2: Input Monitoring

    @MainActor
    private static func checkInputMonitoring() async -> PermissionStatus {
        if CGPreflightListenEventAccess() {
            Log.info("Input Monitoring: already granted.", category: "permissions")
            return .granted
        }

        Log.info("Input Monitoring: requesting access.", category: "permissions")
        let granted = CGRequestListenEventAccess()

        if granted {
            // On macOS 14.4+, even though the call returned true, the grant takes effect
            // only after a process restart. Block the user with a quit-to-reopen sheet.
            if #available(macOS 14.4, *) {
                showQuitAndReopenSheet()
                // showQuitAndReopenSheet calls NSApp.terminate if user clicks Quit Now.
                // If the sheet is dismissed by other means, fall through to .granted anyway
                // so we don't block the rest of the wizard.
            }
            return .granted
        }

        showSettingsAlert(
            title: "Input Monitoring Access Required",
            message: "WisprAlt needs Input Monitoring permission to detect the FN key for dictation. Please enable it in System Settings, then relaunch the app.",
            settingsURL: inputMonitoringURL
        )
        return .denied
    }

    // MARK: - Step 3: Microphone

    @MainActor
    private static func checkMicrophone() async -> PermissionStatus {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)

        switch status {
        case .authorized:
            Log.info("Microphone: already authorized.", category: "permissions")
            return .granted

        case .notDetermined:
            Log.info("Microphone: requesting authorization.", category: "permissions")
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            if granted {
                return .granted
            }
            showSettingsAlert(
                title: "Microphone Access Required",
                message: "WisprAlt needs Microphone access to record audio for transcription. Please enable it in System Settings.",
                settingsURL: microphoneURL
            )
            return .denied

        case .denied, .restricted:
            showSettingsAlert(
                title: "Microphone Access Required",
                message: "WisprAlt needs Microphone access to record audio for transcription. Please enable it in System Settings.",
                settingsURL: microphoneURL
            )
            return .denied

        @unknown default:
            return .unknown
        }
    }

    // MARK: - Step 4: Screen Recording

    @MainActor
    private static func checkScreenRecording() async -> PermissionStatus {
        if CGPreflightScreenCaptureAccess() {
            Log.info("Screen Recording: already granted.", category: "permissions")
            return .granted
        }

        Log.info("Screen Recording: attempting SCShareableContent to trigger TCC prompt.", category: "permissions")
        // Calling SCShareableContent.current() triggers the TCC prompt on macOS 14+.
        do {
            _ = try await SCShareableContent.current()
            Log.info("Screen Recording: granted after SCShareableContent prompt.", category: "permissions")
            return .granted
        } catch {
            Log.warning("SCShareableContent failed (\(error)); falling back to CGRequestScreenCaptureAccess.", category: "permissions")
        }

        // Fallback for older OS paths.
        CGRequestScreenCaptureAccess()

        if CGPreflightScreenCaptureAccess() {
            return .granted
        }

        showSettingsAlert(
            title: "Screen Recording Access Required",
            message: "WisprAlt needs Screen Recording permission to capture system audio for meeting transcription. Please enable it in System Settings.",
            settingsURL: screenRecordingURL
        )
        return .denied
    }

    // MARK: - UI Helpers

    /// Presents a modal NSAlert with an "Open System Settings" button linked to the given URL
    /// and a "Continue Anyway" option.
    @MainActor
    private static func showSettingsAlert(title: String, message: String, settingsURL: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Continue Anyway")

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            if let url = URL(string: settingsURL) {
                NSWorkspace.shared.open(url)
            }
        }
    }

    /// Blocks the UI with a sheet requiring the user to quit and reopen the app.
    /// Called after Input Monitoring is granted on macOS 14.4+ (grant requires process restart).
    @MainActor
    @available(macOS 14.4, *)
    private static func showQuitAndReopenSheet() {
        let alert = NSAlert()
        alert.messageText = "Quit and Reopen Required"
        alert.informativeText =
            "Input Monitoring access has been granted, but it takes effect only after you reopen WisprAlt. " +
            "Please quit and relaunch the app to continue setup."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Quit Now")
        alert.addButton(withTitle: "Later")

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            Log.info("User chose Quit Now after Input Monitoring grant; terminating.", category: "permissions")
            NSApp.terminate(nil)
        }
    }
}
