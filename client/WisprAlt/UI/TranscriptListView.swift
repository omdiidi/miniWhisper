import SwiftUI

/// Lists all locally saved meeting transcripts.
///
/// Observes `TranscriptStore.shared` so the list updates automatically when
/// new transcripts are saved or speakers are renamed.
/// Tapping a row navigates to `TranscriptDetailView` for the selected transcript.
struct TranscriptListView: View {
    // MARK: - State

    @ObservedObject private var store = TranscriptStore.shared
    @State private var selection: String?  // selected job_id

    // MARK: - Body

    var body: some View {
        NavigationSplitView {
            listContent
                .navigationTitle("Meetings")
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        Button(action: { store.refresh() }) {
                            Label("Refresh", systemImage: "arrow.clockwise")
                        }
                        .help("Refresh transcript list")
                    }
                }
        } detail: {
            if let jobID = selection {
                TranscriptDetailView(jobID: jobID)
            } else {
                emptyDetail
            }
        }
    }

    // MARK: - List content

    @ViewBuilder
    private var listContent: some View {
        if store.transcripts.isEmpty {
            ContentUnavailableView(
                "No Meetings Yet",
                systemImage: "waveform.and.mic",
                description: Text("Triple-tap FN to start a meeting recording.")
            )
        } else {
            List(store.transcripts, id: \.id, selection: $selection) { summary in
                TranscriptRowView(summary: summary)
                    .tag(summary.id)
            }
        }
    }

    private var emptyDetail: some View {
        ContentUnavailableView(
            "Select a Meeting",
            systemImage: "doc.text",
            description: Text("Choose a transcript from the list to view it.")
        )
    }
}

// MARK: - Row view

private struct TranscriptRowView: View {
    let summary: TranscriptSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(summary.title)
                .font(.headline)
                .lineLimit(1)

            HStack(spacing: 8) {
                Text(formattedDate(summary.createdAt))
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text(formattedDuration(summary.duration))
                    .font(.caption)
                    .foregroundStyle(.secondary)

                modeBadge
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var modeBadge: some View {
        let label = summary.mode == "in_person" ? "In-Person" : "Remote"
        let color: Color = summary.mode == "in_person" ? .orange : .blue
        Text(label)
            .font(.caption2)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(color.opacity(0.15))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }

    private func formattedDate(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: iso) {
            return date.formatted(date: .abbreviated, time: .shortened)
        }
        return iso
    }

    private func formattedDuration(_ seconds: Double) -> String {
        let total = Int(seconds)
        let m = total / 60
        let s = total % 60
        if m > 0 {
            return "\(m)m \(s)s"
        }
        return "\(s)s"
    }
}

// MARK: - Preview

#Preview {
    TranscriptListView()
        .frame(width: 600, height: 500)
}
