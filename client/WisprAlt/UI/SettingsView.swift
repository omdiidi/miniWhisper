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
    @State private var copyFeedback: String?
    @State private var hasStoredAPIKey: Bool = false

    // Identity (display_name) editing state. Tri-state Optional<String> fixes the
    // snap-back bug:
    //   nil               → field shows the saved value from Settings (read-only display mode)
    //   Some("text")      → user is actively editing
    //   Some("")          → user explicitly cleared the field (will trigger PATCH null on commit)
    @State private var nameDraft: String?
    @State private var savingName: Bool = false
    @State private var nameError: String?

    private enum ConnectionFeedbackKind {
        case neutral, success, warning, error
    }

    // MARK: - Body

    var body: some View {
        Form {
            quickActionsSection
            identitySection
            inputMicSection
            connectionSection
            smartFormattingSection
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
    /// Delegates to `QuickActionsSection` so the file-watcher view-models can
    /// be `@StateObject`-owned (which is impossible inside a computed `var`).
    private var quickActionsSection: some View {
        QuickActionsSection()
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
                Button("Copy API Key") {
                    copyAPIKeyToClipboard()
                }
                .disabled(!hasStoredAPIKey)
                .help(hasStoredAPIKey ? "Copy your API key. Auto-cleared from clipboard after 60 seconds." : "Paste an API key first")

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
            if let msg = copyFeedback {
                Text(msg)
                    .font(.caption)
                    .foregroundStyle(.green)
            }
        }
    }

    /// Identity section — lets the user set a friendly `display_name` that the
    /// admin dashboard will show alongside their token label. Source of truth is
    /// the server (`PATCH /me`); `Settings.shared.displayName` mirrors it locally.
    private var identitySection: some View {
        Section {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Your name")
                    Spacer()
                    TextField(
                        Settings.shared.displayName ?? "Set your name",
                        text: nameBinding
                    )
                    .multilineTextAlignment(.trailing)
                    .textFieldStyle(.plain)
                    .disabled(savingName)
                    .onSubmit {
                        Task { await commitDisplayName() }
                    }
                    .frame(maxWidth: 220)
                }
                if let err = nameError {
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
        } header: { Text("Identity") }
    }

    /// Two-way binding that surfaces the in-progress draft (`nameDraft`) when
    /// editing, and the saved value otherwise. Any keystroke (including clearing
    /// the field) flips into edit mode.
    private var nameBinding: Binding<String> {
        Binding(
            get: {
                nameDraft ?? (settings.displayName ?? "")
            },
            set: { nameDraft = $0 }
        )
    }

    /// Smart-formatting toggle. Off by default. Sends `X-Smart-Format: true` on
    /// `/transcribe/dictate` when on. Server silently ignores the flag if
    /// `OPENROUTER_API_KEY` is not configured.
    private var smartFormattingSection: some View {
        Section {
            Toggle("Smart formatting", isOn: Binding(
                get: { settings.smartFormatting },
                set: { settings.smartFormatting = $0 }
            ))
            Text("Cleans up dictation output (punctuation, casing, paragraph breaks) without changing words. Adds ~250ms latency. Off by default. Requires admin to set OPENROUTER_API_KEY on the server — silently does nothing otherwise.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } header: { Text("Quality") }
    }

    /// Input mic picker — applies ONLY to WisprAlt's own dictation. Meeting
    /// recording uses the macOS system default (SCStream has no per-stream
    /// device API). The picker shows live AVCaptureDevice discovery results.
    private var inputMicSection: some View {
        Section("Input Mic") {
            Picker("", selection: micSelectionBinding) {
                let sysName = MicEnumerator.systemDefaultInputName() ?? "—"
                Text("System Default (\(sysName))").tag(String?.none)
                Divider()
                ForEach(availableMics, id: \.uniqueID) { device in
                    Text(device.name).tag(Optional(device.uniqueID))
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .help("Choose which mic WisprAlt uses for dictation. Meeting recording always uses the macOS system default.")
        }
        .onAppear {
            availableMics = MicEnumerator.availableInputs()
            MicEnumerator.startDeviceListListener()
        }
        .onReceive(NotificationCenter.default.publisher(for: .micDeviceListChanged)) { _ in
            // CoreAudio HAL fired — refresh the picker so AirPods/etc appear live.
            availableMics = MicEnumerator.availableInputs()
        }
    }

    @State private var availableMics: [MicEnumerator.InputDevice] = MicEnumerator.availableInputs()

    /// Two-way binding that maps the picker's `String?` selection to
    /// `Settings.shared.preferredInputDeviceUID`.
    private var micSelectionBinding: Binding<String?> {
        Binding(
            get: { settings.preferredInputDeviceUID },
            set: { settings.preferredInputDeviceUID = $0 }
        )
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
        hasStoredAPIKey = ((try? KeychainHelper.getAPIKey()) ?? nil)?.isEmpty == false
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

    /// Commit the in-progress `nameDraft` to the server via `PATCH /me`.
    ///
    /// On error, `nameDraft` is preserved so the user can retry without retyping
    /// (snap-back bug fix). Only on success is the draft cleared (returning the
    /// field to display mode) and `nameError` reset.
    private func commitDisplayName() async {
        guard let draft = nameDraft, !savingName else { return }
        savingName = true
        defer { savingName = false }  // ONLY toggle saving flag; preserve nameDraft on error
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            if trimmed.isEmpty {
                _ = try await MeAPI.patchDisplayName(nil)
                await MainActor.run { settings.displayName = nil }
            } else if (1...40).contains(trimmed.count) {
                let me = try await MeAPI.patchDisplayName(trimmed)
                await MainActor.run { settings.displayName = me.display_name }
            } else {
                nameError = "Name must be 1-40 characters."
                return  // KEEP draft so user can fix it
            }
            nameDraft = nil   // exit edit mode ONLY on success
            nameError = nil
        } catch {
            Log.warning("display_name update failed: \(error)", category: "settings")
            nameError = "Couldn't save: \(error.localizedDescription)"
            // KEEP nameDraft so user can retry without retyping.
        }
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

    private func copyAPIKeyToClipboard() {
        do {
            guard let key = try KeychainHelper.getAPIKey(), !key.isEmpty else {
                copyFeedback = "No API key to copy."
                return
            }
            let pb = NSPasteboard.general
            pb.clearContents()
            pb.setString(key, forType: .string)
            let mark = pb.changeCount  // post-write changeCount
            copyFeedback = "Copied! Auto-clearing in 60s."
            Log.info("API key copied to clipboard.", category: "settings")

            // Two timers: caption fades fast, clipboard auto-clears slow.
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2 * 1_000_000_000)
                copyFeedback = nil
            }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
                if NSPasteboard.general.changeCount == mark {
                    NSPasteboard.general.clearContents()
                    Log.info("Auto-cleared API key from clipboard (changeCount unchanged).", category: "settings")
                } else {
                    Log.info("Skipped clipboard auto-clear — pasteboard changed.", category: "settings")
                }
            }
        } catch {
            Log.error("Copy API key failed: \(error)", category: "settings")
            copyFeedback = "Copy failed: \(error.localizedDescription)"
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

// MARK: - QuickActionsSection

/// Top-of-popover Section. Owns two `LastTranscriptCaptionViewModel` instances
/// (meetings + custom transcriptions) so they can be `@StateObject`-bound,
/// which is impossible inside `SettingsView`'s computed `quickActionsSection`.
///
/// Lifetime mirrors the popover: watchers `start()` in `.onAppear`, `stop()`
/// in `.onDisappear`. No FD leaks across popover open/close cycles.
private struct QuickActionsSection: View {
    @EnvironmentObject private var settings: Settings
    @EnvironmentObject private var recordingState: RecordingState

    /// Controls presentation of the server-log sheet. Bound to a Bool so the
    /// SwiftUI `.sheet(isPresented:)` lifecycle drives the fetch.
    @State private var showingServerLogSheet: Bool = false

    /// True while the current activeJobID indicates a transcription is in
    /// flight (post-upload, post-cancel-with-server-still-finishing, or
    /// resumed-from-launch). Used to gate Cancel + View-server-log controls.
    private var hasInFlightJob: Bool {
        recordingState.activeJobID != nil
            || recordingState.serverFinishingJobID != nil
            || recordingState.uploadFraction > 0
    }

    /// Id used by the View-server-log sheet. Prefers the active job, falls
    /// back to the finishing-on-server id when the user has cancelled but
    /// the executor is still grinding.
    private var serverLogJobID: String? {
        recordingState.activeJobID ?? recordingState.serverFinishingJobID
    }

    // Known limitation: the watcher folder URLs below are captured ONCE at
    // struct-init time. If the user changes `Settings.shared.meetingsPath`
    // mid-session, the watchers keep pointing at the OLD URL until the popover
    // is destroyed and recreated. Acceptable: meetings-path changes are rare
    // and the popover is `.transient`, so reopening it picks up the new path.
    @StateObject private var meetingViewModel = LastTranscriptCaptionViewModel(
        folderURL: Settings.shared.meetingsPath,
        lookup: {
            CustomTranscriptionsStore.newestMeetingTranscript().flatMap {
                try? $0.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate
            }
        }
    )

    @StateObject private var customViewModel = LastTranscriptCaptionViewModel(
        folderURL: CustomTranscriptionsStore.directoryURL,
        lookup: {
            CustomTranscriptionsStore.newestCustomTranscript().flatMap {
                try? $0.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate
            }
        }
    )

    /// Inline "Copied — N chars" toast. `button` identifies which copy button
    /// fired so the toast renders directly beneath the right one.
    @State private var copyToast: (button: String, message: String)?

    var body: some View {
        Section {
            // Active-job banner / indicator row. Embedded at the top of the
            // section so progress is the first thing the user sees when they
            // open the popover during a transcription. Only visible when an
            // in-flight job is observable on `recordingState`.
            if hasInFlightJob {
                inFlightSection
                Divider()
            }

            Button("Transcribe file…", systemImage: "waveform.badge.plus") {
                MenuBarController.shared?.transcribePickedFile()
            }
            .disabled(recordingState.serverFinishingJobID != nil)
            .help(
                recordingState.serverFinishingJobID != nil
                    ? "A previous transcription is still finishing on the server. New uploads are blocked until it completes."
                    : "Pick any audio or video file. WisprAlt transcodes it locally and runs it through the meeting pipeline."
            )

            Button("Open Custom Transcriptions", systemImage: "folder.badge.questionmark") {
                let url = CustomTranscriptionsStore.directoryURL
                try? FileManager.default.createDirectory(
                    at: url,
                    withIntermediateDirectories: true
                )
                NSWorkspace.shared.open(url)
                Log.info("Opened custom transcriptions folder: \(url.path)", category: "settings")
            }
            .help("Opens the Custom Transcriptions folder in Finder.")

            Divider()

            Button("Copy last meeting", systemImage: "doc.on.clipboard") {
                performCopy(button: "meeting", source: CustomTranscriptionsStore.newestMeetingTranscript)
            }
            .disabled(meetingViewModel.lastModified == nil)
            .help(meetingViewModel.lastModified == nil
                  ? "No meeting transcripts yet."
                  : "Copy the most recent meeting transcript to the clipboard.")
            LastTranscriptCaption(viewModel: meetingViewModel)
            if let toast = copyToast, toast.button == "meeting" {
                Text(toast.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .transition(.opacity)
            }

            Button("Copy last custom transcription", systemImage: "doc.on.clipboard.fill") {
                performCopy(button: "custom", source: CustomTranscriptionsStore.newestCustomTranscript)
            }
            .disabled(customViewModel.lastModified == nil)
            .help(customViewModel.lastModified == nil
                  ? "No custom transcriptions yet."
                  : "Copy the most recent custom transcription to the clipboard.")
            LastTranscriptCaption(viewModel: customViewModel)
            if let toast = copyToast, toast.button == "custom" {
                Text(toast.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .transition(.opacity)
            }

            Divider()

            Button("Open Portal", systemImage: "safari") {
                guard let base = settings.serverURL else { return }
                let url = base.appendingPathComponent("admin/login")
                NSWorkspace.shared.open(url)
                Log.info("Opened portal: \(url.absoluteString)", category: "settings")
            }
            .disabled(settings.serverURL == nil)
            .help(
                settings.serverURL == nil
                    ? "Set a Server URL under Advanced first."
                    : "Opens your portal in the browser. Admins land on the global dashboard, employees on their own usage page."
            )

            Button("Open Meetings Folder", systemImage: "folder") {
                let url = settings.meetingsPath
                try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
                NSWorkspace.shared.open(url)
                Log.info("Opened meetings folder: \(url.path)", category: "settings")
            }
            .help("Opens \(settings.meetingsPath.path) in Finder.")
        }
        .onAppear {
            meetingViewModel.start()
            customViewModel.start()
        }
        .onDisappear {
            meetingViewModel.stop()
            customViewModel.stop()
        }
        .sheet(isPresented: $showingServerLogSheet) {
            ServerLogSheet(jobIDProvider: { serverLogJobID })
        }
    }

    /// "Previous job finishing" banner + Cancel button + View-server-log
    /// button. Rendered only when `hasInFlightJob` is true.
    @ViewBuilder
    private var inFlightSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            if recordingState.serverFinishingJobID != nil {
                Label(
                    "Previous transcription still finishing on server. New uploads will queue.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption)
                .foregroundStyle(.orange)
            } else if let label = recordingState.phaseLabelDisplay {
                Text(transcriptionStatusLine(label: label))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if recordingState.uploadFraction > 0 {
                Text("Uploading… \(Int(recordingState.uploadFraction * 100))%")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 8) {
                Button("Cancel", systemImage: "xmark.circle") {
                    Task { await MenuBarController.shared?.cancelActiveTranscription() }
                }
                .disabled(recordingState.serverFinishingJobID != nil
                          && recordingState.activeJobID == nil)
                .help(
                    recordingState.serverFinishingJobID != nil
                        ? "Cancel was already requested. The server-side executor will finish naturally."
                        : "Cancel the upload (clean) or the in-flight transcription (advisory — server may keep running)."
                )

                Button("View server log", systemImage: "doc.text.magnifyingglass") {
                    showingServerLogSheet = true
                }
                .disabled(serverLogJobID == nil)
                .help("Fetch the server-log slice for this job from /admin/server-log/<id>.")
            }
        }
        .padding(.vertical, 4)
    }

    private func transcriptionStatusLine(label: String) -> String {
        if recordingState.phase == "transcribe",
           let i = recordingState.chunkIndex,
           let n = recordingState.totalChunks, n > 0
        {
            return "\(label) — chunk \(i)/\(n)"
        }
        return label
    }

    private func performCopy(button: String, source: () -> URL?) {
        guard let url = source() else { return }
        do {
            let count = try CustomTranscriptionsStore.copyToPasteboard(url)
            copyToast = (button, "Copied — \(count.formatted(.number)) chars")
        } catch {
            Log.warning("copyToPasteboard failed for \(url.path): \(error)", category: "settings")
            copyToast = (button, "Copy failed: \(error.localizedDescription)")
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if copyToast?.button == button {
                copyToast = nil
            }
        }
    }
}

// MARK: - ServerLogSheet

/// Modal sheet rendering `MeetingAPI.fetchServerLog(_:)` output for an
/// in-flight (or recently-cancelled) job. The job id is fetched lazily
/// through `jobIDProvider` so the sheet can re-read it on Refresh and after
/// `recordingState.activeJobID` flips to `serverFinishingJobID`.
private struct ServerLogSheet: View {
    let jobIDProvider: () -> String?

    @Environment(\.dismiss) private var dismiss
    @State private var logText: String = ""
    @State private var loadError: String? = nil
    @State private var isLoading: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Server log")
                    .font(.headline)
                Spacer()
                Button("Refresh", systemImage: "arrow.clockwise") {
                    Task { await reload() }
                }
                .disabled(isLoading || jobIDProvider() == nil)
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }

            if let err = loadError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            ScrollView {
                Text(logText.isEmpty && isLoading ? "Loading…" : logText)
                    .font(.system(.body, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(8)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .padding()
        .frame(minWidth: 640, minHeight: 480)
        .task { await reload() }
    }

    @MainActor
    private func reload() async {
        guard let raw = jobIDProvider() else {
            loadError = "No active job to fetch logs for."
            return
        }
        isLoading = true
        loadError = nil
        defer { isLoading = false }
        do {
            logText = try await MeetingAPI.fetchServerLog(JobID(raw: raw))
        } catch {
            loadError = "Failed to fetch log: \(error.localizedDescription)"
            Log.warning("ServerLogSheet fetch failed: \(error)", category: "ui")
        }
    }
}
