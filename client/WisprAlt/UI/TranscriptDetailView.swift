import SwiftUI

/// Displays a single meeting transcript with speaker-labelled segments.
///
/// Rename speakers: tap the "Rename Speakers" toolbar button to open a sheet
/// that lists all speakers with their channel badges and allows editing their
/// display names. Rename is validated for collisions via
/// `TranscriptDocument.renameSpeaker(rawKey:to:)` before committing to disk.
///
/// This view is offline-capable — it makes no network calls.
struct TranscriptDetailView: View {
    // MARK: - Properties

    let jobID: String

    // MARK: - State

    @State private var document: TranscriptDocument?
    @State private var loadError: TranscriptError?
    @State private var showRenameSheet = false
    @State private var isBusy = false

    // MARK: - Body

    var body: some View {
        Group {
            if let doc = document {
                transcriptContent(doc)
            } else if let error = loadError {
                errorView(error)
            } else {
                ProgressView("Loading…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .navigationTitle(jobID)
        .navigationSubtitle(document.map { subtitleText($0) } ?? "")
        .toolbar {
            if document != nil {
                ToolbarItem(placement: .primaryAction) {
                    Button("Rename Speakers") {
                        showRenameSheet = true
                    }
                    .disabled(isBusy)
                }
            }
        }
        .sheet(isPresented: $showRenameSheet) {
            if let doc = document {
                RenameSpeakersSheet(
                    document: doc,
                    jobID: jobID,
                    onComplete: { updated in
                        document = updated
                        showRenameSheet = false
                    }
                )
            }
        }
        .task(id: jobID) {
            loadDocument()
        }
    }

    // MARK: - Transcript content

    @ViewBuilder
    private func transcriptContent(_ doc: TranscriptDocument) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                ForEach(Array(doc.segments.enumerated()), id: \.offset) { _, segment in
                    SegmentView(segment: segment)
                }
            }
            .padding()
        }
    }

    private func errorView(_ error: TranscriptError) -> some View {
        ContentUnavailableView(
            "Could Not Load Transcript",
            systemImage: "exclamationmark.triangle",
            description: Text(error.localizedDescription ?? "Unknown error.")
        )
    }

    private func subtitleText(_ doc: TranscriptDocument) -> String {
        let modeLabel = doc.mode == "in_person" ? "In-Person" : "Remote"
        let mins = Int(doc.duration_s / 60)
        return "\(modeLabel) · \(mins) min"
    }

    // MARK: - Load

    private func loadDocument() {
        do {
            document = try TranscriptStore.shared.load(jobID)
            loadError = nil
        } catch let err as TranscriptError {
            loadError = err
            document = nil
        } catch {
            loadError = .ioError(error)
            document = nil
        }
    }
}

// MARK: - Segment view

private struct SegmentView: View {
    let segment: TranscriptDocument.Segment

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Timestamp pill
            Text(timecode(segment.start))
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .frame(width: 56, alignment: .trailing)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 2) {
                // Speaker label with optional channel badge
                HStack(spacing: 4) {
                    Text(segment.speaker)
                        .font(.caption)
                        .bold()
                        .foregroundStyle(.primary)

                    if let channel = segment.channel {
                        Text("ch\(channel)")
                            .font(.caption2)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 1)
                            .background(Color.secondary.opacity(0.15))
                            .clipShape(Capsule())
                    }

                    if segment.overlap {
                        Image(systemName: "arrow.left.and.right.righttriangle.left.righttriangle.right")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .help("Overlapping speech")
                    }
                }

                // Transcript text
                Text(segment.text)
                    .font(.body)
                    .textSelection(.enabled)
            }
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 2)
        .background(segment.overlap ? Color.orange.opacity(0.05) : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 4))
    }

    private func timecode(_ seconds: Double) -> String {
        let total = Int(seconds)
        let m = total / 60
        let s = total % 60
        return String(format: "%d:%02d", m, s)
    }
}

// MARK: - Rename Speakers sheet

private struct RenameSpeakersSheet: View {
    let document: TranscriptDocument
    let jobID: String
    let onComplete: (TranscriptDocument) -> Void

    // MARK: State

    /// Working copy of edited display names, keyed by speaker_raw.
    @State private var editedNames: [String: String] = [:]
    @State private var validationErrors: [String: String] = [:]
    @State private var globalError: String?
    @State private var isSaving = false

    @Environment(\.dismiss) private var dismiss

    // MARK: - Init

    init(document: TranscriptDocument, jobID: String, onComplete: @escaping (TranscriptDocument) -> Void) {
        self.document = document
        self.jobID = jobID
        self.onComplete = onComplete
        // Seed the editable names from the current document.
        var names: [String: String] = [:]
        for (rawKey, info) in document.speakers {
            names[rawKey] = info.display_name
        }
        _editedNames = State(initialValue: names)
    }

    // MARK: - Body

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename Speakers")
                .font(.headline)
                .padding(.top)
                .padding(.horizontal)

            Text("Changes are saved locally. No network connection required.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal)

            if let err = globalError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal)
            }

            Divider()

            ForEach(sortedSpeakers, id: \.rawKey) { speaker in
                speakerRow(speaker)
            }

            Divider()

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)

                Spacer()

                Button("Save") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(isSaving || !validationErrors.isEmpty)
            }
            .padding()
        }
        .frame(minWidth: 380, minHeight: 300)
    }

    // MARK: - Speaker row

    private func speakerRow(_ speaker: SpeakerEntry) -> some View {
        HStack(alignment: .center, spacing: 10) {
            // Channel badge
            if let channel = speaker.channel {
                Text("ch\(channel)")
                    .font(.caption2)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Color.blue.opacity(0.12))
                    .foregroundStyle(.blue)
                    .clipShape(Capsule())
            } else {
                Text("in-person")
                    .font(.caption2)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Color.orange.opacity(0.12))
                    .foregroundStyle(.orange)
                    .clipShape(Capsule())
            }

            // Raw key label (stable, read-only)
            Text(speaker.rawKey)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .frame(minWidth: 80)

            // Editable display name
            VStack(alignment: .leading, spacing: 2) {
                TextField(
                    "Display Name",
                    text: Binding(
                        get: { editedNames[speaker.rawKey] ?? speaker.displayName },
                        set: { newValue in
                            editedNames[speaker.rawKey] = newValue
                            validateName(newValue, for: speaker.rawKey)
                        }
                    )
                )
                .textFieldStyle(.roundedBorder)

                if let err = validationErrors[speaker.rawKey] {
                    Text(err)
                        .font(.caption2)
                        .foregroundStyle(.red)
                }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 4)
    }

    // MARK: - Validation

    private func validateName(_ name: String, for rawKey: String) {
        guard !name.isEmpty else {
            validationErrors[rawKey] = "Name cannot be empty."
            return
        }
        // Check collision with other speakers' edited names.
        let collision = editedNames.contains { key, value in
            key != rawKey && value == name
        }
        if collision {
            validationErrors[rawKey] = "Name already used by another speaker."
        } else {
            validationErrors.removeValue(forKey: rawKey)
        }
    }

    // MARK: - Save

    private func save() {
        // Clear prior global error.
        globalError = nil

        // Determine which speakers actually changed.
        let changedPairs: [(rawKey: String, newName: String)] = editedNames.compactMap { rawKey, newName in
            guard let original = document.speakers[rawKey]?.display_name,
                  original != newName,
                  !newName.isEmpty
            else { return nil }
            return (rawKey, newName)
        }

        guard !changedPairs.isEmpty else {
            // Nothing changed — just dismiss.
            dismiss()
            return
        }

        isSaving = true

        // Apply renames sequentially (in-memory first, then one atomic write per iteration).
        Task { @MainActor in
            defer { isSaving = false }

            // Apply all renames to the store. Each rename call rewrites all four formats.
            for (rawKey, newName) in changedPairs {
                do {
                    try TranscriptStore.shared.renameSpeaker(in: jobID, rawKey: rawKey, to: newName)
                } catch let err as TranscriptError {
                    globalError = err.localizedDescription
                    return
                } catch {
                    globalError = error.localizedDescription
                    return
                }
            }

            // Reload the updated document to pass back to the parent view.
            do {
                let updated = try TranscriptStore.shared.load(jobID)
                onComplete(updated)
            } catch {
                globalError = error.localizedDescription
            }
        }
    }

    // MARK: - Sorted speaker list helper

    private var sortedSpeakers: [SpeakerEntry] {
        document.speakers
            .map { rawKey, info in
                SpeakerEntry(
                    rawKey: rawKey,
                    displayName: info.display_name,
                    channel: info.channel
                )
            }
            .sorted { lhs, rhs in
                // Sort by channel ascending (nil last), then by rawKey.
                switch (lhs.channel, rhs.channel) {
                case let (l?, r?) where l != r: return l < r
                case (nil, _?): return false
                case (_?, nil): return true
                default: return lhs.rawKey < rhs.rawKey
                }
            }
    }

    private struct SpeakerEntry {
        let rawKey: String
        let displayName: String
        let channel: Int?
    }
}

// MARK: - Preview

#Preview {
    TranscriptDetailView(jobID: "sample-job-id")
        .frame(width: 700, height: 600)
}
