import SwiftUI
import ServiceManagement
import AppKit

/// Main settings panel hosted in the menubar popover.
///
/// Layout is user-priority ordered:
///   1. Quick Actions — admin portal + meetings folder shortcuts
///   2. Connection status with a working Test button
///   3. Hotkey timing + launch-at-login (frequently-tweaked)
///   4. Meetings folder picker
///   5. Advanced (collapsed) — server URL, API key, key export/import
struct SettingsView: View {
    @EnvironmentObject private var settings: Settings

    // MARK: - Local ephemeral state

    /// Transient text buffer for the server URL field; validated on submit.
    @State private var serverURLText: String = ""
    /// Transient buffer for the API key SecureField.
    @State private var apiKeyText: String = ""
    /// Validation error message shown below the server URL field.
    @State private var serverURLError: String? = nil
    /// Feedback message shown after Test Connection press.
    @State private var connectionFeedback: String? = nil
    /// Color tag for the feedback message (success/warn/error).
    @State private var connectionFeedbackKind: ConnectionFeedbackKind = .neutral
    /// True while a connection test is in flight.
    @State private var isTesting: Bool = false
    /// File picker presented when "Browse" is tapped.
    @State private var showingFolderPicker: Bool = false
    /// Error message shown below the export/import buttons when an operation fails.
    @State private var exportImportError: String?
    /// Reveals the Server URL / API Key / Export-Import block at the bottom of the form.
    @State private var showAdvanced: Bool = false

    private enum ConnectionFeedbackKind {
        case neutral, success, warning, error
    }

    // MARK: - Body

    var body: some View {
        Form {
            quickActionsSection
            connectionSection
            hotkeySection
            launchAtLoginSection
            meetingsFolderSection
            advancedToggleSection
            if showAdvanced {
                serverSection
                apiKeySection
                apiKeyExportImportSection
            }
        }
        .formStyle(.grouped)
        .padding()
        .frame(width: 420)
        .onAppear(perform: loadCurrentValues)
    }

    // MARK: - Sections

    /// Top of the popover: the actions a daily user actually reaches for.
    private var quickActionsSection: some View {
        Section {
            Button("Open Portal", systemImage: "safari") {
                openPortal()
            }
            .disabled(settings.serverURL == nil)
            .help(
                settings.serverURL == nil
                    ? "Set a Server URL under Advanced first."
                    : "Opens your portal in the browser. Admins land on the global dashboard, employees on their own usage page."
            )

            Button("Open Meetings Folder", systemImage: "folder") {
                openMeetingsFolder()
            }
            .help("Opens \(settings.meetingsPath.path) in Finder.")
        }
    }

    private var advancedToggleSection: some View {
        Section {
            Toggle("Show advanced settings", isOn: $showAdvanced.animation())
                .help("Server URL, API key, and key export/import. Once you're set up, you rarely need these.")
        }
    }

    private var serverSection: some View {
        Section("Server") {
            VStack(alignment: .leading, spacing: 4) {
                TextField("https://transcribe.example.com", text: $serverURLText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(commitServerURL)
                    .autocorrectionDisabled()

                if let err = serverURLError {
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
            .help("The base URL of your WisprAlt server. Must use https.")
        }
    }

    private var apiKeySection: some View {
        Section("API Key") {
            SecureField("Paste API key…", text: $apiKeyText)
                .textFieldStyle(.roundedBorder)
                .onSubmit(commitAPIKey)
            Text("Stored securely in your macOS Keychain.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var meetingsFolderSection: some View {
        Section("Meetings Folder") {
            HStack {
                Text(settings.meetingsPath.path)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)

                Button("Browse…") {
                    showingFolderPicker = true
                }
                .fileImporter(
                    isPresented: $showingFolderPicker,
                    allowedContentTypes: [.folder],
                    onCompletion: handleFolderSelection
                )
            }
        }
    }

    private var hotkeySection: some View {
        Section("Hotkey Timing") {
            LabeledContent("Hold duration (s)") {
                Stepper(
                    value: $settings.holdMinDuration,
                    in: 0.10...1.00,
                    step: 0.05
                ) {
                    Text(String(format: "%.2f", settings.holdMinDuration))
                        .monospacedDigit()
                }
            }
            .help("Minimum FN-hold time before dictation starts.")

            LabeledContent("Triple-tap window (s)") {
                Stepper(
                    value: $settings.tripleTapWindow,
                    in: 0.20...1.00,
                    step: 0.05
                ) {
                    Text(String(format: "%.2f", settings.tripleTapWindow))
                        .monospacedDigit()
                }
            }
            .help("Maximum time between taps to count as a triple-tap.")

            LabeledContent("Max meeting length (min)") {
                Stepper(
                    value: $settings.maxMeetingMinutes,
                    in: 5...240,
                    step: 5
                ) {
                    Text("\(settings.maxMeetingMinutes)")
                        .monospacedDigit()
                }
            }
            .help("Maximum meeting recording duration (5–240 min). Default 90.")
        }
    }

    private var launchAtLoginSection: some View {
        Section("Startup") {
            Toggle("Launch at login", isOn: Binding(
                get:  { settings.launchAtLogin },
                set:  { settings.launchAtLogin = $0 }
            ))
            if SMAppService.mainApp.status == .requiresApproval {
                Button("Open Login Items in System Settings") {
                    SMAppService.openSystemSettingsLoginItems()
                }
            }
        }
    }

    private var apiKeyExportImportSection: some View {
        Section("API Key Backup") {
            HStack {
                Button("Export API Key…") {
                    let panel = NSSavePanel()
                    panel.allowedContentTypes = [.text]
                    panel.nameFieldStringValue = "wispralt-api-key.wispralt-key"
                    panel.directoryURL = FileManager.default.urls(
                        for: .desktopDirectory, in: .userDomainMask
                    ).first
                    panel.message = "Exports your API key. Treat this file like a password."
                    guard panel.runModal() == .OK, let url = panel.url else { return }
                    do {
                        try KeychainHelper.exportAPIKey(to: url)
                        exportImportError = nil
                    } catch {
                        Log.error("API key export failed: \(error)", category: "storage")
                        exportImportError = "Export failed: \(error.localizedDescription)"
                    }
                }

                Button("Import API Key…") {
                    let panel = NSOpenPanel()
                    panel.allowedContentTypes = [.text]
                    panel.directoryURL = FileManager.default.urls(
                        for: .desktopDirectory, in: .userDomainMask
                    ).first
                    panel.message = "Importing replaces any existing API key in the Keychain."
                    guard panel.runModal() == .OK, let url = panel.url else { return }
                    do {
                        try KeychainHelper.importAPIKey(from: url)
                        exportImportError = nil
                        // Refresh the in-memory text field so the UI reflects the new key.
                        do {
                            apiKeyText = (try KeychainHelper.getAPIKey()) ?? ""
                        } catch {
                            Log.error("Failed to re-read API key after import: \(error)", category: "settings")
                        }
                    } catch {
                        Log.error("API key import failed: \(error)", category: "storage")
                        exportImportError = "Import failed: \(error.localizedDescription)"
                    }
                }
            }

            Text("Save exports to your Desktop, not Documents (Documents may sync to iCloud).")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let msg = exportImportError {
                Text(msg)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }

    private var connectionSection: some View {
        Section("Connection") {
            HStack {
                Button(action: testConnection) {
                    if isTesting {
                        ProgressView()
                            .controlSize(.small)
                            .padding(.trailing, 4)
                    }
                    Text(isTesting ? "Testing…" : "Test Connection")
                }
                .disabled(isTesting || settings.serverURL == nil)

                if let feedback = connectionFeedback {
                    Text(feedback)
                        .font(.caption)
                        .foregroundStyle(feedbackColor)
                        .lineLimit(2)
                }
            }
        }
    }

    private var feedbackColor: Color {
        switch connectionFeedbackKind {
        case .success: return .green
        case .warning: return .orange
        case .error:   return .red
        case .neutral: return .secondary
        }
    }

    // MARK: - Actions

    private func loadCurrentValues() {
        serverURLText = settings.serverURL?.absoluteString ?? ""
        // Load API key from Keychain (not from Settings).
        do {
            apiKeyText = (try KeychainHelper.getAPIKey()) ?? ""
        } catch {
            Log.error("Failed to read API key from Keychain: \(error)", category: "settings")
        }
    }

    private func commitServerURL() {
        serverURLError = nil
        let trimmed = serverURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            settings.serverURL = nil
            return
        }
        guard let url = URL(string: trimmed), url.scheme == "https" else {
            serverURLError = "URL must begin with https://"
            return
        }
        settings.serverURL = url
        Log.info("Server URL saved: \(url.absoluteString)", category: "settings")
    }

    private func commitAPIKey() {
        let trimmed = apiKeyText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            try KeychainHelper.setAPIKey(trimmed)
            Log.info("API key saved to Keychain.", category: "settings")
        } catch {
            Log.error("Failed to save API key: \(error)", category: "settings")
        }
    }

    private func handleFolderSelection(_ result: Result<URL, Error>) {
        switch result {
        case .success(let url):
            // Gain a security-scoped bookmark for sandboxed access (future hardened runtime use).
            _ = url.startAccessingSecurityScopedResource()
            settings.meetingsPath = url
            Log.info("Meetings folder set to: \(url.path)", category: "settings")
        case .failure(let error):
            Log.error("Folder picker failed: \(error)", category: "settings")
        }
    }

    /// Test Connection: calls `/healthz`, `/readyz/dictation`, `/readyz/meeting` in parallel and
    /// surfaces a single status line. Green = both ready, orange = healthy but a pipeline still
    /// loading, red = host unreachable or auth bad.
    private func testConnection() {
        guard settings.serverURL != nil else {
            connectionFeedback = "Set a Server URL under Advanced first."
            connectionFeedbackKind = .error
            return
        }
        isTesting = true
        connectionFeedback = nil
        connectionFeedbackKind = .neutral

        Task {
            let client = ServerClient.shared
            do {
                async let healthOK = client.healthz()
                async let dictationReady = client.readyz(endpoint: "dictation")
                async let meetingReady = client.readyz(endpoint: "meeting")
                let h = try await healthOK
                let d = try await dictationReady
                let m = try await meetingReady

                await MainActor.run {
                    isTesting = false
                    if !h {
                        connectionFeedback = "Server reachable but /healthz failed."
                        connectionFeedbackKind = .error
                        return
                    }
                    if d.ok && m.ok {
                        connectionFeedback = d.degraded
                            ? "Connected — dictation degraded (a meeting is using memory)"
                            : "Connected — dictation + meeting ready."
                        connectionFeedbackKind = d.degraded ? .warning : .success
                        return
                    }
                    if d.ok && !m.ok {
                        connectionFeedback = "Connected — meeting pipeline still loading."
                        connectionFeedbackKind = .warning
                        return
                    }
                    if !d.ok && m.ok {
                        connectionFeedback = "Connected — dictation pipeline still loading."
                        connectionFeedbackKind = .warning
                        return
                    }
                    connectionFeedback = "Connected — pipelines still loading."
                    connectionFeedbackKind = .warning
                }
            } catch ServerError.unauthorized {
                await MainActor.run {
                    isTesting = false
                    connectionFeedback = "API key rejected. Re-paste it under Advanced."
                    connectionFeedbackKind = .error
                }
            } catch {
                await MainActor.run {
                    isTesting = false
                    connectionFeedback = "Failed: \(error.localizedDescription)"
                    connectionFeedbackKind = .error
                }
            }
        }
    }

    /// Open the portal landing page (`/admin/login`) in the user's default browser.
    /// The server's role-based redirect sends admins to /admin/ and employees to
    /// /admin/me, so the same button works for everyone.
    private func openPortal() {
        guard let base = settings.serverURL else { return }
        let url = base.appendingPathComponent("admin/login")
        NSWorkspace.shared.open(url)
        Log.info("Opened portal: \(url.absoluteString)", category: "settings")
    }

    /// Reveal the meetings folder in Finder.
    private func openMeetingsFolder() {
        let url = settings.meetingsPath
        // Make sure the directory exists; opening a non-existent path silently no-ops.
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.open(url)
        Log.info("Opened meetings folder: \(url.path)", category: "settings")
    }
}
