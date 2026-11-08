import SwiftUI
import ServiceManagement
import AppKit
import WisprAltCore

/// Lightweight Apple-glass card used for the active-job indicator and other
/// emphasis surfaces inside the popover. Kept private + inline because the
/// classic-style `WisprAlt.xcodeproj` PBXFileReference tables would otherwise
/// need an edit to include a new .swift file.
private struct GlassCard<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View {
        content
            .padding(12)
            .background(
                .thinMaterial,
                in: RoundedRectangle(cornerRadius: 14, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
            )
    }
}

/// Main settings panel hosted in the menubar popover.
///
/// Layout is user-priority ordered:
///   1. Mode picker — Online (OpenAI) / Local server
///   2. Quick Actions — file transcription, admin portal + transcripts folder shortcuts
///   3. Connection status with a working Test button (Local)
///   4. Hotkey timing + launch-at-login (frequently-tweaked)
///   5. Transcripts folder picker
///   6. Advanced (collapsed) — OpenAI model + key; server URL, API key, key export/import (Local)
///
/// In Online every server-only row is hidden behind one `isLocal` predicate.
struct SettingsView: View {
    @EnvironmentObject private var settings: Settings
    @EnvironmentObject private var recordingState: RecordingState

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
    /// Reveals the Server URL / API Key block at the bottom of the form.
    @State private var showAdvanced: Bool = false
    /// Shown for ~5s when UpdateChecker.triggerInstall() couldn't open Terminal
    /// (TCC denied / Terminal missing) and fell back to copying the install
    /// command to the clipboard. Listens for `.updaterFallbackToClipboard`.
    @State private var updateFallbackToastVisible: Bool = false
    /// Shown for ~5s when the user pressed "Install now…" while a dictation
    /// or file job was in progress. Listens for `.updaterDictationBusy`.
    @State private var updateBusyToastVisible: Bool = false

    // Identity (display_name) editing state. Tri-state Optional<String> fixes the
    // snap-back bug:
    //   nil               → field shows the saved value from Settings (read-only display mode)
    //   Some("text")      → user is actively editing
    //   Some("")          → user explicitly cleared the field (will trigger PATCH null on commit)
    @State private var nameDraft: String?
    @State private var savingName: Bool = false
    @State private var nameError: String?

    // OpenAI key entry (Advanced). The field never shows the stored key.
    @State private var openAIKeyText: String = ""
    @State private var hasOpenAIKey: Bool = false
    @State private var openAIKeyError: String?
    @FocusState private var openAIKeyFocused: Bool
    /// Last Local `/healthz` result for the status line (nil = not checked yet).
    /// Checked only on popover open in Local or on a switch to Local.
    @State private var localHealthy: Bool? = nil

    /// Server-only rows (connection, server, API key, identity, portal links,
    /// server log) exist only in Local mode.
    private var isLocal: Bool { settings.mode == .local }

    private enum ConnectionFeedbackKind {
        case neutral, success, warning, error
    }

    // MARK: - Body

    var body: some View {
        Form {
            modeSection
            quickActionsSection
            if isLocal {
                connectionSection
            }
            inputMicSection
            smartFormattingSection
            // No streaming toggle: Local dictation always streams; Online never does.
            hotkeySection
            launchAtLoginSection
            transcriptsFolderSection
            advancedToggleSection
            if showAdvanced {
                updateSection
                openAIModelSection
                openAIKeySection
                if isLocal {
                    serverSection
                    apiKeySection
                }
                aboutAndQuitSection
            }
            if isLocal {
                identitySection
            }
        }
        .formStyle(.grouped)
        .frame(width: 360)
        .onAppear {
            loadCurrentValues()
            handleKeyEntryRequest()
            checkLocalHealth()
        }
        .onChange(of: settings.mode) { _, _ in checkLocalHealth() }
        .onChange(of: recordingState.keyEntryRequested) { _, _ in handleKeyEntryRequest() }
    }

    // MARK: - Sections

    /// Online (OpenAI) / Local server. A change applies to the next dictation;
    /// locked while a Local upload has no server job id yet.
    private var modeSection: some View {
        Section {
            Picker("Mode", selection: Binding(
                get: { settings.mode },
                set: { MenuBarController.shared?.applyModeSwitch(to: $0) }
            )) {
                Text("Online (OpenAI)").tag(TranscriptionMode.online)
                Text("Local server").tag(TranscriptionMode.local)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .disabled(recordingState.localUploadPhase.locksModePicker)
            if recordingState.localUploadPhase.locksModePicker {
                Text("Finish or cancel the upload first")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(statusLine)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// `Online · <model> · key saved|key missing` (key presence only, no
    /// network) or `Local · <host>` (+ last `/healthz`), plus "transcribing N".
    private var statusLine: String {
        var parts: [String]
        if isLocal {
            parts = ["Local", settings.serverURL?.host() ?? "no server set"]
            if let ok = localHealthy { parts.append(ok ? "reachable" : "unreachable") }
        } else {
            parts = ["Online", settings.openAIModel, hasOpenAIKey ? "key saved" : "key missing"]
        }
        if recordingState.dictationsInFlight > 0 {
            parts.append("transcribing \(recordingState.dictationsInFlight)")
        }
        return parts.joined(separator: " · ")
    }

    /// OpenAI model picker: id, small price text and retire tag per row.
    /// Applies to the next dictation (the snapshot is taken at FN press).
    private var openAIModelSection: some View {
        Section("OpenAI Model") {
            Picker("Model", selection: $settings.openAIModel) {
                ForEach(OpenAIModels.all) { m in
                    HStack(spacing: 6) {
                        Text(m.id)
                        Text(m.displayPrice).font(.caption).foregroundStyle(.secondary)
                        if let tag = m.retireTag {
                            Text(tag).font(.caption).foregroundStyle(.orange)
                        }
                    }
                    .tag(m.id)
                }
            }
            .pickerStyle(.radioGroup)
            .labelsHidden()
            Text("Files always use a file model. Dictation stops at "
                 + "\(OpenAIModels.resolve(id: settings.openAIModel).dictationCapSeconds / 60) min for this model.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// OpenAI key (Keychain `co.wispralt.openai` / `default`). Saved via the
    /// field or from Terminal; either works while the app runs.
    private var openAIKeySection: some View {
        Section("OpenAI API Key") {
            HStack {
                SecureField(hasOpenAIKey ? "Key saved — paste to replace" : "Paste OpenAI API key…",
                            text: $openAIKeyText)
                    .textFieldStyle(.roundedBorder)
                    .focused($openAIKeyFocused)
                    .onSubmit(saveOpenAIKey)
                Button("Save", action: saveOpenAIKey)
                    .disabled(openAIKeyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("Remove", role: .destructive, action: removeOpenAIKey)
                    .disabled(!hasOpenAIKey)
            }
            Text(hasOpenAIKey ? "Key saved" : "Key missing")
                .font(.caption)
                .foregroundStyle(hasOpenAIKey ? .green : .orange)
            Text("Or from Terminal:")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("security add-generic-password -U -s co.wispralt.openai -a default -w")
                .font(.caption.monospaced())
                .textSelection(.enabled)
            if let err = openAIKeyError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }

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

    /// Where file-transcription outputs are saved (`Settings.meetingsPath`;
    /// the property/key name is kept so existing transcripts stay put).
    private var transcriptsFolderSection: some View {
        Section("Transcripts Folder") {
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

    /// Update awareness — shows "Update available: vX.Y.Z" + Install button when
    /// `Settings.shared.updateAvailable` is set; otherwise shows the current
    /// version + a manual "Check for updates" button. Set by UpdateChecker.
    private var updateSection: some View {
        Section("Updates") {
            if let remote = settings.updateAvailable {
                HStack {
                    Image(systemName: "arrow.down.circle.fill")
                        .foregroundStyle(.orange)
                    Text("Update available: v\(remote)")
                        .font(.body.weight(.medium))
                    Spacer()
                    Button("Install now…") {
                        UpdateChecker.shared.triggerInstall()
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.orange)
                }
                Text("Opens Terminal to download and install the latest WisprAlt. The current version will quit during the install.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                HStack {
                    Text("You're on v\(Self.appVersionString)")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Check for updates") {
                        Task { await UpdateChecker.shared.check(force: true) }
                    }
                    .buttonStyle(.bordered)
                }
            }
            if updateFallbackToastVisible {
                Text("Couldn't open Terminal — install command copied to clipboard. Paste it into a new Terminal window.")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .transition(.opacity)
            }
            if updateBusyToastVisible {
                Text("Finish your current dictation or file transcription before installing — the installer will quit the app.")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .transition(.opacity)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .updaterFallbackToClipboard)) { _ in
            updateFallbackToastVisible = true
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 5 * 1_000_000_000)
                updateFallbackToastVisible = false
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .updaterDictationBusy)) { _ in
            updateBusyToastVisible = true
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 5 * 1_000_000_000)
                updateBusyToastVisible = false
            }
        }
    }

    /// About + Quit — bottom of the advanced section. Shows the installed
    /// WisprAlt version and a one-click full-quit (NSApp.terminate). v0.4.5+.
    private var aboutAndQuitSection: some View {
        Section {
            HStack {
                Text("Version")
                    .foregroundStyle(.secondary)
                Spacer()
                Text(Self.appVersionString)
                    .font(.system(.body, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Button(role: .destructive) {
                Log.info("User quit WisprAlt from Settings.", category: "app")
                NSApp.terminate(nil)
            } label: {
                Label("Quit WisprAlt", systemImage: "power")
                    .frame(maxWidth: .infinity, alignment: .center)
            }
            .buttonStyle(.bordered)
        }
    }

    private static var appVersionString: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return short == build ? short : "\(short)+\(build)"
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
                        "Set your name",
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

    /// Smart-formatting toggle. Off by default. Adds light cleanup to longer
    /// dictations (punctuation, capitalization, paragraph breaks) without
    /// changing the words you said. Adds a small amount of latency.
    private var smartFormattingSection: some View {
        Section {
            Toggle("Smart formatting", isOn: Binding(
                get: { settings.smartFormatting },
                set: { settings.smartFormatting = $0 }
            ))
            Text("Adds light cleanup — punctuation, capitalization, paragraph breaks — to longer dictations. Your wording is preserved. Off by default.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } header: { Text("Quality") }
    }

    /// Input mic picker — applies ONLY to WisprAlt's own dictation. The picker
    /// shows live AVCaptureDevice discovery results.
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
            .help("Choose which mic WisprAlt uses for dictation.")
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
        hasOpenAIKey = OpenAIKey.current() != nil
        serverURLText = settings.serverURL?.absoluteString ?? ""
        // The Local key is read only in Local mode (its rows are hidden in Online).
        guard isLocal else { return }
        // Load API key from Keychain (not from Settings).
        do {
            apiKeyText = (try KeychainHelper.getAPIKey()) ?? ""
        } catch {
            Log.error("Failed to read API key from Keychain: \(ErrorLog.describe(error))", category: "settings")
        }
    }

    /// Local `/healthz` for the status line. Never runs in Online (the Local
    /// host is not contacted there).
    private func checkLocalHealth() {
        localHealthy = nil
        guard isLocal, settings.serverURL != nil else { return }
        Task {
            let ok = (try? await ServerClient.shared.healthz()) ?? false
            await MainActor.run { if isLocal { localHealthy = ok } }
        }
    }

    /// FN pressed in Online with no key: reveal Advanced and focus the key field.
    private func handleKeyEntryRequest() {
        guard recordingState.keyEntryRequested else { return }
        recordingState.keyEntryRequested = false
        showAdvanced = true
        DispatchQueue.main.async { openAIKeyFocused = true }
    }

    private func saveOpenAIKey() {
        let trimmed = openAIKeyText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            try OpenAIKey.save(trimmed)
            openAIKeyText = ""
            openAIKeyError = nil
            hasOpenAIKey = true
            Log.info("OpenAI key saved to Keychain.", category: "settings")
        } catch {
            Log.error("OpenAI key save failed: \(ErrorLog.describe(error))", category: "settings")
            openAIKeyError = "Could not save the key to the Keychain."
        }
    }

    private func removeOpenAIKey() {
        do {
            try OpenAIKey.remove()
            openAIKeyError = nil
            hasOpenAIKey = false
            Log.info("OpenAI key removed from Keychain.", category: "settings")
        } catch {
            Log.error("OpenAI key remove failed: \(ErrorLog.describe(error))", category: "settings")
            openAIKeyError = "Could not remove the key from the Keychain."
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
        Log.info("Server URL saved.", category: "settings")
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
            Log.warning("display_name update failed: \(ErrorLog.describe(error))", category: "settings")
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
            Log.error("Failed to save API key: \(ErrorLog.describe(error))", category: "settings")
        }
    }

    private func handleFolderSelection(_ result: Result<URL, Error>) {
        switch result {
        case .success(let url):
            // Gain a security-scoped bookmark for sandboxed access (future hardened runtime use).
            _ = url.startAccessingSecurityScopedResource()
            settings.meetingsPath = url
            Log.info("Transcripts folder set.", category: "settings")
        case .failure(let error):
            Log.error("Folder picker failed: \(ErrorLog.describe(error))", category: "settings")
        }
    }

    /// Test Connection: end-to-end probe that actually verifies each surface a
    /// dictation depends on. v0.4.6 — added authenticated `/me` call so an
    /// invalid API key is detected (previously only /healthz + /readyz which
    /// are no-auth, so a wrong key silently passed Test Connection).
    ///
    /// Probes in parallel:
    ///   - /healthz                  — server process alive at all
    ///   - /readyz/dictation         — Parakeet model loaded
    ///   - /me (authenticated)       — API key + Supabase user lookup work
    ///
    /// Surfaces a single status line color-coded green/orange/red.
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
                async let meAuth = MeAPI.get()      // verifies API key end-to-end

                let h = try await healthOK
                let d = try await dictationReady
                let me = try await meAuth           // throws .unauthorized on bad key

                await MainActor.run {
                    isTesting = false
                    if !h {
                        connectionFeedback = "Server reachable but /healthz failed."
                        connectionFeedbackKind = .error
                        return
                    }
                    let who = me.display_name ?? me.label ?? "you"
                    if d.ok {
                        connectionFeedback = d.degraded
                            ? "Connected as \(who) — dictation degraded (file job using memory)"
                            : "Connected as \(who) — dictation ready."
                        connectionFeedbackKind = d.degraded ? .warning : .success
                        return
                    }
                    connectionFeedback = "Connected as \(who) — dictation pipeline still loading."
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
        Log.info("Opened portal.", category: "settings")
    }
}

// MARK: - QuickActionsSection

/// Top-of-popover Section. Owns the custom-transcriptions
/// `LastTranscriptCaptionViewModel` so it can be `@StateObject`-bound,
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
            || recordingState.uploadError != nil
    }

    /// Id used by the View-server-log sheet. Prefers the active job, falls
    /// back to the finishing-on-server id when the user has cancelled but
    /// the executor is still grinding.
    private var serverLogJobID: String? {
        recordingState.activeJobID ?? recordingState.serverFinishingJobID
    }

    // Known limitation: the watcher folder URL below is captured ONCE at
    // struct-init time. If the user changes `Settings.shared.meetingsPath`
    // mid-session, the watcher keeps pointing at the OLD URL until the popover
    // is destroyed and recreated. Acceptable: transcripts-folder changes are
    // rare and the popover is `.transient`, so reopening it picks up the new path.
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

    /// Server-only rows are hidden (or disabled) in Online.
    private var isLocal: Bool { settings.mode == .local }

    var body: some View {
        Section {
            // v0.5.0: surface most-recent server-persisted dictation so the
            // user can re-paste it without rummaging through the portal.
            // Reads the Local server, so it is disabled in Online.
            Button("Copy last dictation", systemImage: "doc.on.clipboard") {
                performLastDictationCopy()
            }
            .buttonStyle(.bordered)
            .disabled(!isLocal)
            .help(isLocal
                  ? "Copies the text of your most recent persisted dictation. May lag your current dictation by a second or two while it finalizes server-side."
                  : "Available in Local mode only for now.")

            if let toast = copyToast, toast.button == "last-dictation" {
                Text(toast.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .transition(.opacity)
            }

            // Active-job banner / indicator row. Embedded at the top of the
            // section so progress is the first thing the user sees when they
            // open the popover during a transcription. Only visible when an
            // in-flight job is observable on `recordingState`.
            if hasInFlightJob {
                inFlightSection
            }

            Button("Transcribe file…", systemImage: "waveform.badge.plus") {
                MenuBarController.shared?.transcribePickedFile()
            }
            .buttonStyle(.borderedProminent)
            .tint(.accentColor)
            .disabled(!isLocal || recordingState.serverFinishingJobID != nil)
            .help(
                recordingState.serverFinishingJobID != nil
                    ? "A previous transcription is still finishing on the server. New uploads are blocked until it completes."
                    : "Pick any audio or video file. WisprAlt extracts the audio locally and the server transcribes it."
            )
            if !isLocal {
                Text("File transcription in Online arrives with this mission's file step — switch to Local")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            // Last-custom copy + age on one row.
            HStack(spacing: 6) {
                Button("Copy last custom transcription", systemImage: "doc.on.clipboard.fill") {
                    performCopy(button: "custom", source: CustomTranscriptionsStore.newestCustomTranscript)
                }
                .buttonStyle(.bordered)
                .disabled(customViewModel.lastModified == nil)
                .help(customViewModel.lastModified == nil
                      ? "No custom transcriptions yet."
                      : "Copy the most recent custom transcription to the clipboard.")
                Spacer(minLength: 8)
                LastTranscriptCaption(viewModel: customViewModel)
            }
            if let toast = copyToast, toast.button == "custom" {
                Text(toast.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .transition(.opacity)
            }

            // v0.4.6 ordering: Open File Transcriptions sits right above Open
            // Transcripts Folder (the two "show me my outputs" buttons paired). Open Portal moved
            // to the bottom of the list (least-frequently-clicked of the three).
            Button("Open File Transcriptions", systemImage: "folder.badge.questionmark") {
                let url = CustomTranscriptionsStore.directoryURL
                try? FileManager.default.createDirectory(
                    at: url,
                    withIntermediateDirectories: true
                )
                NSWorkspace.shared.open(url)
                Log.info("Opened custom transcriptions folder.", category: "settings")
            }
            .buttonStyle(.bordered)
            .help("Opens the File Transcriptions folder in Finder.")

            Button("Open Transcripts Folder", systemImage: "folder") {
                let url = settings.meetingsPath
                try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
                NSWorkspace.shared.open(url)
                Log.info("Opened transcripts folder.", category: "settings")
            }
            .buttonStyle(.bordered)
            .help("Opens \(settings.meetingsPath.path) in Finder.")

            if isLocal {
                serverLinks
            }
        }
        .onAppear {
            customViewModel.start()
        }
        .onDisappear {
            customViewModel.stop()
        }
        .sheet(isPresented: $showingServerLogSheet) {
            ServerLogSheet(jobIDProvider: { serverLogJobID })
        }
    }

    /// Open Portal + Open My Dictations (Local only).
    @ViewBuilder
    private var serverLinks: some View {
        Button("Open Portal", systemImage: "safari") {
            guard let base = settings.serverURL else { return }
            let url = base.appendingPathComponent("admin/login")
            NSWorkspace.shared.open(url)
            Log.info("Opened portal.", category: "settings")
        }
        .buttonStyle(.bordered)
        .disabled(settings.serverURL == nil)
        .help(
            settings.serverURL == nil
                ? "Set a Server URL under Advanced first."
                : "Opens your portal in the browser. Admins land on the global dashboard, employees on their own usage page."
        )

        // v0.5.0: deep-link through /me/login?next=/me/history so first-time users
        // land directly on their history after auth.
        Button("Open My Dictations", systemImage: "list.bullet.rectangle") {
            guard let base = settings.serverURL else { return }
            var comps = URLComponents(url: base.appendingPathComponent("me/login"),
                                      resolvingAgainstBaseURL: false)
            comps?.queryItems = [URLQueryItem(name: "next", value: "/me/history")]
            guard let url = comps?.url else { return }
            NSWorkspace.shared.open(url)
            Log.info("Opened /me/login?next=/me/history.", category: "settings")
        }
        .buttonStyle(.bordered)
        .disabled(settings.serverURL == nil)
        .help(
            settings.serverURL == nil
                ? "Set a Server URL under Advanced first."
                : "Opens your personal dictation + transcription history in the browser."
        )
    }

    /// Active-job glass card: animated headline, real progress (chunked
    /// transcribe or upload fraction), and Cancel + View-server-log controls.
    /// Rendered only when `hasInFlightJob` is true.
    @ViewBuilder
    private var inFlightSection: some View {
        GlassCard {
            // Error state is mutually exclusive with the active-job UI —
            // showing both a red "stalled" banner AND a "Processing" spinner
            // is contradictory. When `uploadError` is set the job is already
            // torn down by `cancelActiveTranscription`, so render error-only.
            if let uploadError = recordingState.uploadError {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                        .imageScale(.large)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Upload failed")
                            .font(.body.weight(.medium))
                            .foregroundStyle(.red)
                        Text(uploadError)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 4)
                    Button {
                        recordingState.uploadError = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help("Dismiss")
                }
            } else {
                activeJobBody
            }
        }
        .padding(.vertical, 4)
    }

    /// Original active-job content (extracted so the error state can render
    /// alone without nested conditionals).
    @ViewBuilder
    private var activeJobBody: some View {
        VStack(alignment: .leading, spacing: 10) {
                // Headline row
                HStack(spacing: 8) {
                    Image(systemName: "waveform")
                        .foregroundStyle(.tint)
                        .symbolEffect(.variableColor.iterative, isActive: true)
                    Text(headlineText)
                        .font(.body.weight(.medium))
                    Spacer(minLength: 8)
                    if let elapsed = recordingState.phaseElapsedS {
                        Text(formatElapsed(elapsed))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }

                // Progress
                if recordingState.serverFinishingJobID != nil
                    && recordingState.activeJobID == nil {
                    // Cancelled-but-server-still-finishing: indeterminate.
                    ProgressView()
                        .progressViewStyle(.linear)
                        .tint(.orange)
                    Text("Previous transcription still finishing on server. New uploads will queue.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                } else if recordingState.phase == "transcribe",
                          let idx = recordingState.chunkIndex,
                          let total = recordingState.totalChunks, total > 0 {
                    ProgressView(value: Double(idx), total: Double(total))
                        .progressViewStyle(.linear)
                        .tint(.accentColor)
                    Text("Chunk \(idx) of \(total)")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                } else if recordingState.isFinalizing {
                    // Chunked-upload `/finalize` in flight: server is concat'ing
                    // chunks (1-10s on big files). Indeterminate bar is more
                    // honest than "Uploading 99%".
                    ProgressView()
                        .progressViewStyle(.linear)
                        .tint(.accentColor)
                    Text("Finalizing")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if recordingState.uploadFraction > 0 && recordingState.uploadFraction < 1 {
                    ProgressView(value: recordingState.uploadFraction)
                        .progressViewStyle(.linear)
                        .tint(.blue)
                    Text("Uploading — \(Int(recordingState.uploadFraction * 100))%")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                } else {
                    ProgressView()
                        .progressViewStyle(.linear)
                        .tint(.accentColor)
                }

                // Actions
                HStack(spacing: 8) {
                    Button(role: .destructive) {
                        Task { await MenuBarController.shared?.cancelActiveTranscription() }
                    } label: {
                        Label("Cancel", systemImage: "xmark.circle")
                    }
                    .buttonStyle(.bordered)
                    .tint(.red)
                    .disabled(recordingState.serverFinishingJobID != nil
                              && recordingState.activeJobID == nil)
                    .help(
                        recordingState.serverFinishingJobID != nil
                            ? "Cancel was already requested. The server-side executor will finish naturally."
                            : "Cancel the upload (clean) or the in-flight transcription (advisory — server may keep running)."
                    )

                    if isLocal {
                        Button {
                            showingServerLogSheet = true
                        } label: {
                            Label("Log", systemImage: "doc.text.magnifyingglass")
                        }
                        .buttonStyle(.bordered)
                        .disabled(serverLogJobID == nil)
                        .help("Fetch the server-log slice for this job from /admin/server-log/<id>.")
                    }
                }
            }
    }

    /// Headline shown in the glass card. Falls back through the same priority
    /// ladder the old caption row used.
    private var headlineText: String {
        if recordingState.serverFinishingJobID != nil
            && recordingState.activeJobID == nil {
            return "Finishing previous job"
        }
        if recordingState.isFinalizing {
            return "Finalizing"
        }
        if let label = recordingState.phaseLabelDisplay {
            return label
        }
        if recordingState.uploadFraction > 0 && recordingState.uploadFraction < 1 {
            return "Uploading"
        }
        return "Processing"
    }

    /// Format elapsed seconds as `m:ss` (or `s` when under a minute).
    private func formatElapsed(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s)s" }
        let m = s / 60
        let r = s % 60
        return String(format: "%d:%02d", m, r)
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
            Log.warning("copyToPasteboard failed: \(ErrorLog.describe(error))", category: "settings")
            copyToast = (button, "Copy failed: \(error.localizedDescription)")
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if copyToast?.button == button {
                copyToast = nil
            }
        }
    }

    private func performLastDictationCopy() {
        Task { @MainActor in
            do {
                let last = try await LastDictationAPI.fetch()
                let pb = NSPasteboard.general
                pb.clearContents()
                pb.setString(last.text, forType: .string)
                copyToast = ("last-dictation", "Copied — \(last.text.count.formatted(.number)) chars")
            } catch ServerError.server(status: 404, _) {
                copyToast = ("last-dictation", "No dictations yet — say something first.")
            } catch ServerError.server(status: 403, _) {
                copyToast = ("last-dictation", "Break-glass admin has no personal dictations.")
            } catch ServerError.unauthorized {
                copyToast = ("last-dictation", "API key rejected.")
            } catch {
                Log.warning("Copy-last-dictation failed: \(ErrorLog.describe(error))", category: "settings")
                copyToast = ("last-dictation", "Couldn't fetch — try again.")
            }
            try? await Task.sleep(nanoseconds: 1_800_000_000)
            if copyToast?.button == "last-dictation" { copyToast = nil }
        }
    }
}

// MARK: - ServerLogSheet

/// Modal sheet rendering `FileJobAPI.fetchServerLog(_:)` output for an
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
            logText = try await FileJobAPI.fetchServerLog(JobID(raw: raw))
        } catch let error as ServerError {
            if case .server(let status, _) = error, status == 403 {
                loadError = "Server logs are admin-only. Ask your administrator for help."
            } else {
                loadError = "Failed to fetch log: \(error.localizedDescription)"
            }
            Log.warning("ServerLogSheet fetch failed: \(ErrorLog.describe(error))", category: "ui")
        } catch {
            loadError = "Failed to fetch log: \(error.localizedDescription)"
            Log.warning("ServerLogSheet fetch failed: \(ErrorLog.describe(error))", category: "ui")
        }
    }
}
