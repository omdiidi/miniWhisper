import Combine
import Foundation

// MARK: - RecordingState

/// Lightweight ObservableObject that carries upload-progress state into the SwiftUI
/// popover. `MenuBarController` owns the single instance and mutates it on the main
/// actor; SwiftUI views observe it via `@EnvironmentObject`.
final class RecordingState: ObservableObject {
    /// Upload fraction in [0.0, 1.0]. Drives `RecordingIndicatorView(.uploading(_))`.
    @Published var uploadFraction: Double = 0

    /// Timestamp of the most recent upload-progress callback. Updated every
    /// time `uploadFraction` advances; read by the stall watchdog in
    /// `MenuBarController.startUploadWatchdog()`. Nil between uploads.
    @Published var lastUploadProgressAt: Date? = nil

    /// User-visible upload error, set by the stall watchdog when a wedged
    /// upload is aborted. The popover renders this as a red banner inside
    /// `inFlightSection`. Cleared by `MenuBarController` at the start of
    /// every new upload and by the user dismissing the banner.
    @Published var uploadError: String? = nil

    // MARK: - Per-phase progress (server-driven)

    /// Raw phase name from the server (`ffprobe`, `transcribe`, `diarize`, …).
    @Published var phase: String? = nil
    /// Friendly phase label sent by the server (`"Transcribing"`, …). Falls
    /// back to the local map via `phaseLabelDisplay` when absent.
    @Published var phaseLabel: String? = nil
    /// 1-based chunk progress during `transcribe`. Nil outside that phase.
    @Published var chunkIndex: Int? = nil
    /// Total chunk count during `transcribe`. Nil outside that phase.
    @Published var totalChunks: Int? = nil
    /// Seconds since the current phase started (server-side wall clock).
    @Published var phaseElapsedS: Double? = nil
    /// Total audio duration in seconds, populated after server-side ffprobe.
    @Published var audioDurationS: Double? = nil
    /// Currently-active server job id. Persisted to UserDefaults so a process
    /// restart can resume polling. Nil when no job is in flight.
    @Published var activeJobID: String? = nil
    /// Set when the user cancelled but the server-side executor is still
    /// running an in-pipeline phase. The popover shows a "Previous job
    /// finishing" banner and blocks new submissions while this is non-nil.
    @Published var serverFinishingJobID: String? = nil

    /// True while the chunked uploader's `/finalize` step is in flight (server-
    /// side chunk concatenation). The popover replaces the "Uploading 99%"
    /// label with an indeterminate "Finalizing" progress bar so the UI doesn't
    /// lie about progress during the concat window (can be 1-10s on big files).
    @Published var isFinalizing: Bool = false

    /// Reset every per-job field. Caller is responsible for also clearing
    /// the UserDefaults `activeJobID` key — `reset()` only touches in-memory
    /// state so it can be called from contexts that shouldn't write defaults.
    func reset() {
        uploadFraction = 0
        lastUploadProgressAt = nil
        uploadError = nil
        phase = nil
        phaseLabel = nil
        chunkIndex = nil
        totalChunks = nil
        phaseElapsedS = nil
        audioDurationS = nil
        activeJobID = nil
        isFinalizing = false
    }

    /// Friendly phase label: uses the server-provided `phaseLabel` when set,
    /// otherwise looks up `phase` in a static map; falls back to the raw
    /// phase name. Nil only when no phase has been observed yet.
    var phaseLabelDisplay: String? {
        if let server = phaseLabel, !server.isEmpty { return server }
        guard let p = phase else { return nil }
        return Self.phaseLabelMap[p] ?? p
    }

    private static let phaseLabelMap: [String: String] = [
        "queued": "Waiting in queue",
        "starting": "Starting",
        "ffprobe": "Inspecting audio",
        "ffmpeg_decode": "Decoding audio",
        "transcribe_load": "Loading transcription model",
        "transcribe": "Transcribing",
        "diarize_load": "Loading speaker model",
        "diarize": "Identifying speakers",
        "merge": "Finalizing transcript",
        "output_write": "Writing outputs",
    ]
}
