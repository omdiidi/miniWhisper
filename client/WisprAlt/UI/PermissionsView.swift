import SwiftUI

/// Visual checklist of the four required permissions.
///
/// Shows a per-permission status row with an "Open System Settings" button.
/// A "Re-check" button re-runs PermissionGate.checkAll() to refresh the UI.
struct PermissionsView: View {
    // MARK: - State

    @State private var statuses: [PermissionRow] = Self.defaultRows()
    @State private var isChecking: Bool = false

    // MARK: - Body

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Required Permissions")
                .font(.headline)
                .padding(.bottom, 4)

            ForEach(statuses.indices, id: \.self) { index in
                permissionRow(statuses[index])
            }

            Divider()

            HStack {
                Button(action: recheck) {
                    if isChecking {
                        ProgressView().controlSize(.small).padding(.trailing, 4)
                    }
                    Text(isChecking ? "Checking…" : "Re-check All")
                }
                .disabled(isChecking)

                Spacer()
            }
        }
        .padding()
        .frame(width: 380)
        .task { await runCheck() }
    }

    // MARK: - Row View

    @ViewBuilder
    private func permissionRow(_ row: PermissionRow) -> some View {
        HStack(spacing: 10) {
            statusIcon(row.status)
                .frame(width: 20)

            VStack(alignment: .leading, spacing: 2) {
                Text(row.name)
                    .fontWeight(.medium)
                Text(row.description)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if row.status != .granted {
                Button("Open Settings") {
                    openSettings(url: row.settingsURL)
                }
                .controlSize(.small)
            }
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private func statusIcon(_ status: PermissionStatus) -> some View {
        switch status {
        case .granted:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .denied:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        case .unknown:
            Image(systemName: "questionmark.circle.fill")
                .foregroundStyle(.orange)
        }
    }

    // MARK: - Actions

    private func recheck() {
        Task { await runCheck() }
    }

    @MainActor
    private func runCheck() async {
        isChecking = true
        let results = await PermissionGate.checkAll()
        // Map results back to rows (same order: a, im, mic, sr).
        for (i, status) in results.enumerated() {
            if i < statuses.count {
                statuses[i].status = status
            }
        }
        isChecking = false
    }

    private func openSettings(url: String) {
        if let settingsURL = URL(string: url) {
            NSWorkspace.shared.open(settingsURL)
        }
    }

    // MARK: - Row model

    private static func defaultRows() -> [PermissionRow] {
        [
            PermissionRow(
                name: "Accessibility",
                description: "Insert transcribed text at your cursor.",
                settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                status: .unknown
            ),
            PermissionRow(
                name: "Input Monitoring",
                description: "Detect FN-key holds and taps for dictation and meeting recording.",
                settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
                status: .unknown
            ),
            PermissionRow(
                name: "Microphone",
                description: "Record your voice for dictation and meeting transcription.",
                settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
                status: .unknown
            ),
            PermissionRow(
                name: "Screen Recording",
                description: "Capture system audio for dual-channel meeting transcription.",
                settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
                status: .unknown
            )
        ]
    }
}

// MARK: - Supporting model

private struct PermissionRow: Identifiable {
    let id = UUID()
    let name: String
    let description: String
    let settingsURL: String
    var status: PermissionStatus
}
