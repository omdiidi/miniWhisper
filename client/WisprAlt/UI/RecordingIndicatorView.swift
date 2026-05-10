import SwiftUI

// MARK: - Phase enum

/// All states the recording/upload/processing indicator can be in.
enum RecordingPhase: Equatable {
    /// No active recording or upload.
    case idle
    /// FN held; mic is actively capturing dictation. `startedAt` used for elapsed display.
    case recording(Date)
    /// Meeting WAV is being uploaded. `fraction` is in `[0.0, 1.0]`.
    case uploading(Double)
    /// Server is processing the uploaded meeting. `startedAt` used for elapsed timer.
    case processing(Date)
    /// Pipeline finished. Shown briefly before returning to idle.
    case done
}

// MARK: - View

/// Compact indicator shown in the menubar popover during active operations.
///
/// States:
///   - `idle`       — hidden / empty.
///   - `recording`  — red pulsing mic icon + elapsed time.
///   - `uploading`  — upload icon + percentage + progress bar.
///   - `processing` — waveform animation + elapsed timer.
///   - `done`       — green checkmark, auto-dismisses after 2 s.
struct RecordingIndicatorView: View {
    // MARK: - Input

    let phase: RecordingPhase

    // MARK: - State

    /// Server-driven per-phase progress (phase label + chunk index). Injected
    /// from the popover root. Previews supply a stub via
    /// `.environmentObject(RecordingState())`.
    @EnvironmentObject private var recordingState: RecordingState

    /// Drives the elapsed timer for `.recording` and `.processing` states.
    @State private var now: Date = .now
    private let timer = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    // MARK: - Body

    var body: some View {
        Group {
            switch phase {
            case .idle:
                EmptyView()
            case .recording(let startDate):
                recordingRow(startDate: startDate)
            case .uploading(let fraction):
                uploadingRow(fraction: fraction)
            case .processing(let startDate):
                processingRow(startDate: startDate)
            case .done:
                doneRow
            }
        }
        .onReceive(timer) { tick in
            now = tick
        }
        .animation(.easeInOut(duration: 0.25), value: phase)
    }

    // MARK: - State rows

    private func recordingRow(startDate: Date) -> some View {
        HStack(spacing: 8) {
            // Pulsing red mic
            Image(systemName: "mic.fill")
                .foregroundStyle(.red)
                .symbolEffect(.pulse, isActive: true)

            VStack(alignment: .leading, spacing: 1) {
                Text("Recording")
                    .font(.caption.bold())
                Text(elapsed(since: startDate))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private func uploadingRow(fraction: Double) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "icloud.and.arrow.up")
                .foregroundStyle(.blue)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Uploading")
                        .font(.caption.bold())
                    Spacer()
                    Text("\(Int(fraction * 100))%")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                ProgressView(value: fraction)
                    .progressViewStyle(.linear)
                    .tint(.blue)
            }
        }
        .padding(.vertical, 4)
    }

    private func processingRow(startDate: Date) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "waveform")
                .foregroundStyle(.purple)
                .symbolEffect(.variableColor.iterative, isActive: true)

            VStack(alignment: .leading, spacing: 1) {
                Text(processingHeadline)
                    .font(.caption.bold())
                HStack(spacing: 3) {
                    Text("Elapsed:")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(elapsed(since: startDate))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }

    /// Headline string for the processing row. Renders the friendly phase
    /// label (server-provided when present, else the local map) and appends
    /// `chunk i/n` ONLY while the server is in the `transcribe` phase with a
    /// non-zero total. Falls back to "Processing" before any phase has been
    /// observed.
    private var processingHeadline: String {
        guard let label = recordingState.phaseLabelDisplay else { return "Processing" }
        if recordingState.phase == "transcribe",
           let i = recordingState.chunkIndex,
           let n = recordingState.totalChunks, n > 0
        {
            return "\(label) — chunk \(i)/\(n)"
        }
        return label
    }

    private var doneRow: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
            Text("Transcript saved")
                .font(.caption.bold())
        }
        .padding(.vertical, 4)
    }

    // MARK: - Helpers

    /// Returns a `MM:SS` string representing the elapsed time since `date`.
    private func elapsed(since date: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        let m = seconds / 60
        let s = seconds % 60
        return String(format: "%d:%02d", m, s)
    }
}

// MARK: - Preview

#Preview("Idle") {
    RecordingIndicatorView(phase: .idle)
        .padding()
        .frame(width: 260)
        .environmentObject(RecordingState())
}

#Preview("Recording") {
    RecordingIndicatorView(phase: .recording(Date()))
        .padding()
        .frame(width: 260)
        .environmentObject(RecordingState())
}

#Preview("Uploading 43%") {
    RecordingIndicatorView(phase: .uploading(0.43))
        .padding()
        .frame(width: 260)
        .environmentObject(RecordingState())
}

#Preview("Processing") {
    RecordingIndicatorView(phase: .processing(Date()))
        .padding()
        .frame(width: 260)
        .environmentObject(RecordingState())
}

#Preview("Processing — Transcribing chunks") {
    let state = RecordingState()
    state.phase = "transcribe"
    state.phaseLabel = "Transcribing"
    state.chunkIndex = 7
    state.totalChunks = 21
    return RecordingIndicatorView(phase: .processing(Date()))
        .padding()
        .frame(width: 260)
        .environmentObject(state)
}

#Preview("Done") {
    RecordingIndicatorView(phase: .done)
        .padding()
        .frame(width: 260)
        .environmentObject(RecordingState())
}
