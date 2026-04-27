import SwiftUI

/// First-launch "What should we call you?" dialog.
///
/// Hosted in a standalone NSWindow (NOT a SwiftUI `.sheet` on the popover —
/// NSPopover + .sheet is broken on macOS 15). Reads coordinator state via
/// `@EnvironmentObject` so this view works as the contentView of a window.
struct DisplayNameSheet: View {
    @EnvironmentObject var coordinator: FirstLaunchCoordinator
    @State private var nameInput: String = ""
    @State private var saving: Bool = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("What should we call you?")
                .font(.headline)
            Text("Your name appears in WisprAlt's admin user list. You can change it anytime in Settings.")
                .font(.callout)
                .foregroundStyle(.secondary)

            TextField("Your name", text: $nameInput)
                .textFieldStyle(.roundedBorder)
                .disabled(saving)
                .onSubmit { Task { await save() } }

            if let err = errorMessage {
                Text(err).foregroundStyle(.red).font(.caption)
            }

            HStack {
                Button("Skip later") { coordinator.recordSkip() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Spacer()
                Button(saving ? "Saving…" : "Save") {
                    Task { await save() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(
                    saving
                    || nameInput.trimmingCharacters(in: .whitespacesAndNewlines).count < 1
                    || nameInput.trimmingCharacters(in: .whitespacesAndNewlines).count > 40
                )
            }
        }
        .padding(20)
        .frame(width: 360)
    }

    private func save() async {
        let trimmed = nameInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (1...40).contains(trimmed.count) else {
            errorMessage = "Name must be 1-40 characters."
            return
        }
        saving = true
        defer { saving = false }
        do {
            let me = try await MeAPI.patchDisplayName(trimmed)
            Settings.shared.displayName = me.display_name
            coordinator.recordSave()
        } catch {
            errorMessage = "Couldn't save: \(error.localizedDescription)"
        }
    }
}
