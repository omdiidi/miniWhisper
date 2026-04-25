import SwiftUI

/// Main settings panel hosted in the menubar popover.
///
/// Fields:
///   - Server URL (https only; validated on submit)
///   - API key (SecureField; stored in Keychain on commit — never in UserDefaults)
///   - Meetings folder (browse button)
///   - Hold duration Stepper
///   - Triple-tap window Stepper
///   - Test Connection button (wired to ServerClient.healthz by Wave 1b)
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
    /// True while a connection test is in flight.
    @State private var isTesting: Bool = false
    /// File picker presented when "Browse" is tapped.
    @State private var showingFolderPicker: Bool = false

    // MARK: - Body

    var body: some View {
        Form {
            serverSection
            apiKeySection
            meetingsFolderSection
            hotkeySection
            connectionSection
        }
        .formStyle(.grouped)
        .padding()
        .frame(width: 400)
        .onAppear(perform: loadCurrentValues)
    }

    // MARK: - Sections

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
        }
    }

    private var connectionSection: some View {
        Section {
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
                        .foregroundStyle(feedback.hasPrefix("OK") ? Color.green : Color.red)
                }
            }
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

    /// Test Connection: calls ServerClient.healthz and both /readyz endpoints.
    /// The actual network call is wired by Wave 1b's ServerClient. For now the button
    /// is visible and the action logs, so the integration point is clear.
    private func testConnection() {
        // Wave 1b wires ServerClient.healthz() here.
        print("test connection — wired by Wave 1b")
        isTesting = true
        connectionFeedback = nil

        // Placeholder async simulation so the spinner shows briefly.
        Task {
            try? await Task.sleep(nanoseconds: 500_000_000)
            await MainActor.run {
                isTesting = false
                connectionFeedback = "Wave 1b will wire this."
            }
        }
    }
}
