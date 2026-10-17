import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

// MARK: - File transcription: upload, poll, cancel, resume

extension MenuBarController {
    /// Probe a source file's audio duration via AVAsset. Returns nil if the
    /// container is unreadable or has no audio track. Used to size the
    /// poll deadline in `runFileTranscriptionJob`.
    static func probeAudioDuration(_ url: URL) async -> TimeInterval? {
        let asset = AVURLAsset(url: url)
        do {
            let duration = try await asset.load(.duration)
            let seconds = CMTimeGetSeconds(duration)
            guard seconds.isFinite, seconds > 0 else { return nil }
            return seconds
        } catch {
            return nil
        }
    }

    /// Submit an original-container source file via `POST /transcribe/file`,
    /// poll until done, download every reported format to
    /// `<outputDirectory>/<stem>.<fmt>`, delete the server-side job.
    /// Throws on failure; never enqueues for offline retry — caller decides.
    ///
    /// Deadline scales with the source's audio duration (probed via AVAsset)
    /// so a 5-minute clip and a 4-hour meeting both get realistic budgets.
    /// Multiplier accounts for mlx-whisper + Pyannote on Mac mini M4 running
    /// at roughly 0.3-0.5× realtime for stereo diarization. On timeout we
    /// LEAVE the server-side job in place so the user can recover/poll it
    /// later — deleting it would throw away an in-flight transcription.
    /// Used by both the meeting upload path (`processMeetingUpload`) and
    /// the custom-transcription path (`processCustomTranscriptionUpload`).
    @MainActor
    func runFileTranscriptionJob(
        sourceURL: URL,
        outputDirectory: URL,
        stem: String,
        mode: String
    ) async throws {
        // --- Step 0: extract audio if the source is a video container ---
        // A 200 MB MP4 typically becomes a ~15 MB .m4a — often dropping below
        // the chunked-upload threshold entirely. Failures degrade gracefully
        // back to the original URL (AudioExtractor never throws).
        let extractStart = Date()
        let sourceBytes: Int64 = (
            ((try? FileManager.default.attributesOfItem(atPath: sourceURL.path)[.size]) as? NSNumber)?.int64Value
        ) ?? 0
        let uploadURL = await AudioExtractor.extractAudioIfVideo(sourceURL)
        let extractedBytes: Int64 = (
            ((try? FileManager.default.attributesOfItem(atPath: uploadURL.path)[.size]) as? NSNumber)?.int64Value
        ) ?? 0
        let extractElapsedMs = Int(Date().timeIntervalSince(extractStart) * 1000)
        if uploadURL != sourceURL {
            Log.info(
                "AudioExtractor: input=\(sourceBytes) B → output=\(extractedBytes) B in \(extractElapsedMs)ms",
                category: "transcribe"
            )
        } else {
            Log.info(
                "AudioExtractor: no extraction (input=\(sourceBytes) B, elapsed=\(extractElapsedMs)ms)",
                category: "transcribe"
            )
        }
        let extractedTempDir: URL? = (uploadURL != sourceURL)
            ? uploadURL.deletingLastPathComponent()
            : nil
        defer {
            // Always clean up the extraction temp dir, success or failure.
            if let dir = extractedTempDir {
                try? FileManager.default.removeItem(at: dir)
            }
        }

        // Probe audio duration BEFORE upload so we can size the poll deadline.
        // Falls back to a generous 4-hour ceiling if probing fails (corrupt
        // header, unsupported container) — server will reject upload anyway
        // if it really can't decode.
        let durationSeconds = await Self.probeAudioDuration(uploadURL)
        let deadlineSeconds: TimeInterval
        if let dur = durationSeconds {
            // 3× realtime + 5 min overhead, floor 600s, ceiling 6h.
            deadlineSeconds = max(600, min(6 * 3600, dur * 3 + 300))
            Log.info("File deadline sized: duration=\(Int(dur))s → poll budget=\(Int(deadlineSeconds))s", category: "transcribe")
        } else {
            deadlineSeconds = 4 * 3600
            Log.warning("File duration probe failed — falling back to 4h poll budget.", category: "transcribe")
        }

        // --- Upload ---
        // Reset stall-watchdog state before bytes start flowing and arm the
        // 30-second no-progress abort.
        recordingState.uploadError = nil
        // Seed a tiny positive fraction + an initial progress timestamp so the
        // popover renders the upload card immediately (the visibility gate in
        // SettingsView keys off `uploadFraction > 0`). Without this seed the
        // user sees a blank popover during the `/init` round-trip on chunked
        // uploads, and during the multipart-envelope build for single-shot.
        recordingState.uploadFraction = 0.001
        recordingState.lastUploadProgressAt = Date()
        recordingState.isFinalizing = false
        startUploadWatchdog()

        // Pick the upload path based on file size. The 50 MiB threshold leaves
        // ~50 MB margin under Cloudflare's 100 MB request-body cap.
        let uploadSize: Int64 = (
            ((try? FileManager.default.attributesOfItem(atPath: uploadURL.path)[.size]) as? NSNumber)?.int64Value
        ) ?? 0

        let jobID: JobID
        do {
            if uploadSize > ChunkedUploader.chunkThreshold {
                Log.info(
                    "File >\(ChunkedUploader.chunkThreshold / (1024 * 1024))MB (\(uploadSize) bytes) — using chunked upload.",
                    category: "transcribe"
                )
                jobID = try await ChunkedUploader.upload(
                    fileURL: uploadURL,
                    mode: mode,
                    progress: { [weak self] fraction in
                        Task { @MainActor [weak self] in
                            guard let self else { return }
                            self.recordingState.uploadFraction = fraction
                            self.recordingState.lastUploadProgressAt = Date()
                        }
                    },
                    sessionRegistered: { [weak self] session in
                        Task { @MainActor [weak self] in
                            self?.activeUploadSession = session
                        }
                    },
                    phaseHandler: { [weak self] phase in
                        Task { @MainActor [weak self] in
                            guard let self else { return }
                            switch phase {
                            case .initRequest, .chunk:
                                self.recordingState.isFinalizing = false
                            case .finalize:
                                self.recordingState.isFinalizing = true
                            }
                        }
                    }
                )
            } else {
                jobID = try await MeetingAPI.submitFile(
                    uploadURL,
                    mode: mode,
                    progress: { [weak self] fraction in
                        guard let self else { return }
                        self.recordingState.uploadFraction = fraction
                        self.recordingState.lastUploadProgressAt = Date()
                    },
                    sessionRegistered: { [weak self] session in
                        // The continuation closure that creates the URLSession is
                        // @Sendable; hop to the main actor to satisfy isolation on
                        // the controller's `activeUploadSession` field.
                        Task { @MainActor [weak self] in
                            self?.activeUploadSession = session
                        }
                    }
                )
            }
        } catch {
            // Tear down watchdog + session refs before surfacing the error so
            // the stall watchdog doesn't fire on a torn-down upload.
            uploadWatchdogTask?.cancel()
            uploadWatchdogTask = nil
            activeUploadSession = nil
            recordingState.lastUploadProgressAt = nil
            recordingState.isFinalizing = false
            recordingState.uploadFraction = 0
            // Only set the user-facing banner when the watchdog didn't already
            // populate it (the watchdog sets a stall-specific message and
            // calls `cancelActiveTranscription`, which throws `.cancelled`).
            let isCancellation: Bool
            if let urlErr = error as? URLError, urlErr.code == .cancelled {
                isCancellation = true
            } else {
                isCancellation = (error is CancellationError)
            }
            if !isCancellation && recordingState.uploadError == nil {
                recordingState.uploadError = uploadErrorMessage(for: error)
            }
            throw error
        }
        // Upload finished cleanly — drop the session reference + watchdog.
        // Cancel paths also clear these in `cancelActiveTranscription()`.
        uploadWatchdogTask?.cancel()
        uploadWatchdogTask = nil
        activeUploadSession = nil
        recordingState.lastUploadProgressAt = nil
        recordingState.isFinalizing = false
        // Make the job id observable + persistent so a process restart can
        // resume polling and the UI can render Cancel + View-server-log.
        recordingState.activeJobID = jobID.raw
        UserDefaults.standard.set(jobID.raw, forKey: Self.activeJobIDDefaultsKey)

        // --- Processing ---
        self.mode = .processing
        self.recordingState.uploadFraction = 0  // clear so "Uploading 100%" stops showing
        Log.info("File uploaded — job_id: \(jobID), polling for completion.", category: "transcribe")

        let pollDeadline = Date(timeIntervalSinceNow: deadlineSeconds)

        var outputFormats: [String] = []
        pollLoop: while true {
            if Date() > pollDeadline {
                Log.error("File poll timed out for job \(jobID) after \(Int(deadlineSeconds))s — server-side job left in place for manual recovery.", category: "transcribe")
                throw MeetingProcessingError.pollTimedOut
            }
            try await Task.sleep(nanoseconds: 5_000_000_000)
            let status = try await pollWithBackoff(jobID)
            switch status {
            case .done(let outputs):
                outputFormats = Array(outputs.keys)
                break pollLoop
            case .failed(let reason):
                throw MeetingProcessingError.serverFailed(reason)
            case .pending:
                continue
            case .running(let progress, let serverFinishing):
                recordingState.phase = progress?.phase
                recordingState.phaseLabel = progress?.phaseLabel
                recordingState.chunkIndex = progress?.chunkIndex
                recordingState.totalChunks = progress?.totalChunks
                recordingState.phaseElapsedS = progress?.phaseElapsedS
                recordingState.audioDurationS = progress?.audioDurationS
                if serverFinishing {
                    // User-initiated cancel was acknowledged on the server,
                    // but an in-pipeline phase is still running. Surface this
                    // through the popover banner.
                    recordingState.serverFinishingJobID = recordingState.activeJobID
                }
                continue
            }
        }
        // Job completed cleanly — clear per-job UI state. The download +
        // delete steps below are quick and don't need progress fields.
        recordingState.phase = nil
        recordingState.phaseLabel = nil
        recordingState.chunkIndex = nil
        recordingState.totalChunks = nil

        // --- Download all formats ---
        let formatsToDownload: [String]
        if !outputFormats.isEmpty {
            formatsToDownload = outputFormats.sorted()
        } else {
            formatsToDownload = ["json", "srt", "vtt", "txt"]
        }
        let baseURL = outputDirectory.appendingPathComponent(stem)
        for fmt in formatsToDownload {
            let data = try await MeetingAPI.download(jobID, format: fmt)
            try data.write(to: baseURL.appendingPathExtension(fmt), options: .atomic)
        }

        // --- Cleanup ---
        try await MeetingAPI.delete(jobID)
        recordingState.activeJobID = nil
        UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
    }

    /// Poll the job with exponential backoff (1 s / 2 s / 4 s) on transient
    /// `URLError`s — offline blips and timeouts should not surface as an
    /// immediate processing failure. After 3 consecutive transient failures
    /// the underlying error is re-thrown so the upper-level catch can route
    /// to the offline queue or surface the failure to the user.
    @MainActor
    func pollWithBackoff(_ jobID: JobID) async throws -> JobStatus {
        let backoffsNs: [UInt64] = [1_000_000_000, 2_000_000_000, 4_000_000_000]
        var lastError: Error?
        for attempt in 0..<(backoffsNs.count + 1) {
            do {
                return try await MeetingAPI.poll(jobID)
            } catch let urlError as URLError {
                lastError = urlError
                Log.warning(
                    "Poll transient error (attempt \(attempt + 1)): \(urlError.code.rawValue) — backing off.",
                    category: "transcribe"
                )
            } catch ServerError.transport(let underlying) {
                lastError = underlying
                Log.warning(
                    "Poll transport error (attempt \(attempt + 1)): \(underlying.localizedDescription) — backing off.",
                    category: "transcribe"
                )
            }
            if attempt < backoffsNs.count {
                try await Task.sleep(nanoseconds: backoffsNs[attempt])
            }
        }
        throw lastError ?? ServerError.transport(URLError(.unknown))
    }

    // MARK: - Upload stall watchdog

    /// Poll `recordingState.lastUploadProgressAt` every 5s; if it hasn't
    /// advanced in 120s while an upload is in-flight (`mode == .uploading`
    /// and `uploadFraction < 1.0`), abort the upload via
    /// `cancelActiveTranscription()` and surface a user-visible error on
    /// `recordingState.uploadError`. The watchdog self-terminates after
    /// firing, when cancelled, or when the upload completes (caller clears it).
    ///
    /// 120s threshold (was 30s): large multi-hundred-MB file uploads behind
    /// cloudflared can legitimately pause for 30-60s while buffering or
    /// during TCP backpressure spikes. The user prefers "wait longer, then
    /// abort" over "abort prematurely on a slow connection."
    @MainActor
    func startUploadWatchdog() {
        uploadWatchdogTask?.cancel()
        uploadWatchdogTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                guard let self else { return }
                if Task.isCancelled { return }
                guard let lastProgress = self.recordingState.lastUploadProgressAt else { continue }
                let stalledFor = Date().timeIntervalSince(lastProgress)
                let stillUploading = self.recordingState.uploadFraction < 1.0
                    && self.mode == .uploading
                    && self.activeUploadSession != nil
                if stalledFor > 120, stillUploading {
                    Log.warning(
                        "Upload stalled \(Int(stalledFor))s with fraction=\(self.recordingState.uploadFraction) — aborting.",
                        category: "transcribe"
                    )
                    await self.cancelActiveTranscription()
                    self.recordingState.uploadError = "Upload stalled for 2 minutes. Check your connection and retry."
                    return
                }
            }
        }
    }

    // MARK: - Cancel + resume

    /// Cancel whatever transcription is currently running. Mid-upload cancel
    /// is clean (URLSession is invalidated, no server-side state created).
    /// Mid-transcribe cancel is *advisory* — the server-side executor thread
    /// keeps running until it finishes naturally; `serverFinishingJobID` is
    /// set on `recordingState` so the popover banner appears.
    @MainActor
    func cancelActiveTranscription() async {
        Log.info("Cancel requested — tearing down active transcription.", category: "transcribe")

        // 1) Cancel the in-process Task — stops the poll loop on the next
        //    cooperative yield point.
        activeJobTask?.cancel()
        activeJobTask = nil

        // 2) Cancel the stall watchdog so it doesn't fire after we've
        //    already torn down the upload.
        uploadWatchdogTask?.cancel()
        uploadWatchdogTask = nil

        // 3) Drop the upload session, if any — kills any in-flight bytes.
        activeUploadSession?.invalidateAndCancel()
        activeUploadSession = nil
        recordingState.lastUploadProgressAt = nil

        // 3) Tell the server to stop. The DELETE is best-effort; on success
        //    we set `serverFinishingJobID` so the UI knows the cancel was
        //    acknowledged (and that in-pipeline work may continue).
        if let raw = recordingState.activeJobID {
            let jobID = JobID(raw: raw)
            do {
                try await MeetingAPI.cancel(jobID)
                // Move the id from "active" to "server finishing" so the
                // popover banner appears and new submissions are blocked.
                // Mid-transcribe cancel is advisory — the server may still
                // be running an in-pipeline phase that cannot be interrupted.
                recordingState.serverFinishingJobID = raw
                Log.warning(
                    "Cancel is advisory for in-pipeline phases — server may keep running until natural completion.",
                    category: "transcribe"
                )
            } catch {
                Log.warning(
                    "MeetingAPI.cancel(\(raw)) failed: \(error.localizedDescription)",
                    category: "transcribe"
                )
            }
        }

        // 4) Reset local UI state and forget the persisted id. The
        //    serverFinishingJobID (if set) is preserved on recordingState so
        //    the banner stays visible.
        recordingState.activeJobID = nil
        UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
        recordingState.phase = nil
        recordingState.phaseLabel = nil
        recordingState.chunkIndex = nil
        recordingState.totalChunks = nil
        recordingState.phaseElapsedS = nil
        recordingState.uploadFraction = 0
        recordingState.isFinalizing = false
        self.mode = .idle
    }

    /// On app launch: if UserDefaults remembers an in-flight job id, set it
    /// on `recordingState` so the popover renders Cancel + View-server-log,
    /// and spawn a polling Task that drains the job to completion (or
    /// surfaces `serverFinishingJobID` if it's still grinding).
    ///
    /// Called from `AppDelegate.applicationDidFinishLaunching` after
    /// `MenuBarController` is constructed. Safe to call when no id is stored
    /// — early-returns silently.
    @MainActor
    func resumeInFlightJobIfNeeded() {
        guard let raw = UserDefaults.standard.string(forKey: Self.activeJobIDDefaultsKey),
              !raw.isEmpty else { return }
        Log.info("Resuming polling for in-flight job: \(raw)", category: "transcribe")
        recordingState.activeJobID = raw
        self.mode = .processing
        self.recordingState.uploadFraction = 0  // clear so "Uploading 100%" stops showing
        activeJobTask = Task { @MainActor [weak self] in
            await self?.resumePollingForJob(JobID(raw: raw))
        }
    }

    /// Drain a recovered job. Polls until done/failed and writes a synthetic
    /// "resumed" notification on completion. No download is performed —
    /// recovery is best-effort; the user can manually open the meetings
    /// folder if outputs were written elsewhere.
    ///
    /// Note: a resumed job has lost its caller-known `outputDirectory` and
    /// `stem`. We surface completion to the user but the actual downloads
    /// stay on the server until the user triggers a fresh transcription
    /// (or until the server's GC sweeps them). This is intentional — the
    /// recovery path is for visibility, not for completing the original
    /// transaction.
    @MainActor
    func resumePollingForJob(_ jobID: JobID) async {
        let deadlineSeconds: TimeInterval = 6 * 3600
        let pollDeadline = Date(timeIntervalSinceNow: deadlineSeconds)
        do {
            while true {
                if Date() > pollDeadline {
                    Log.warning("Resumed-job poll timed out for \(jobID).", category: "transcribe")
                    break
                }
                try await Task.sleep(nanoseconds: 5_000_000_000)
                let status = try await pollWithBackoff(jobID)
                switch status {
                case .done:
                    AppNotifications.notify(
                        title: "Transcription Recovered",
                        body: "A job that was running when WisprAlt restarted has completed. Re-run the transcription to download outputs."
                    )
                    recordingState.activeJobID = nil
                    UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
                    self.mode = .idle
                    return
                case .failed(let reason):
                    Log.warning("Resumed-job failed: \(reason)", category: "transcribe")
                    recordingState.activeJobID = nil
                    UserDefaults.standard.removeObject(forKey: Self.activeJobIDDefaultsKey)
                    self.mode = .idle
                    return
                case .pending:
                    continue
                case .running(let progress, let serverFinishing):
                    recordingState.phase = progress?.phase
                    recordingState.phaseLabel = progress?.phaseLabel
                    recordingState.chunkIndex = progress?.chunkIndex
                    recordingState.totalChunks = progress?.totalChunks
                    recordingState.phaseElapsedS = progress?.phaseElapsedS
                    recordingState.audioDurationS = progress?.audioDurationS
                    if serverFinishing {
                        recordingState.serverFinishingJobID = jobID.raw
                    }
                    continue
                }
            }
        } catch {
            Log.warning(
                "Resumed-job polling aborted: \(error.localizedDescription)",
                category: "transcribe"
            )
        }
    }

    /// Map a thrown upload error to a user-readable banner string. Switches
    /// on `ChunkedUploaderError` cases so the user sees a specific cause
    /// (file-too-large vs init vs chunk vs finalize) and falls back to the
    /// raw `localizedDescription` for everything else (URLError, ServerError,
    /// `MeetingAPI.submitFile` failures, …).
    @MainActor
    func uploadErrorMessage(for error: Error) -> String {
        if let chunked = error as? ChunkedUploaderError {
            switch chunked {
            case .fileTooLarge:
                return "File too large (limit 4 GB). Try compressing or splitting."
            case .invalidFile:
                return "Upload failed: cannot read the source file."
            case .chunkUploadFailed(let underlying):
                return "Upload failed: \(underlying.localizedDescription)"
            case .initFailed(let underlying):
                return "Couldn't start upload: \(underlying.localizedDescription)"
            case .finalizeFailed(let underlying):
                return "Upload assembled but server rejected: \(underlying.localizedDescription)"
            }
        }
        if case ServerError.unauthorized = error {
            return "Authentication failed — re-paste your API key in Settings."
        }
        return "Upload failed: \(error.localizedDescription)"
    }

    /// Map a thrown transcription error to a user-facing message. Shared
    /// between the meeting and custom-transcription catch paths so
    /// `ServerError.unauthorized` always surfaces the same actionable hint.
    @MainActor
    func formatTranscriptionError(_ error: Error) -> String {
        if case ServerError.unauthorized = error {
            return "Authentication failed — re-paste your API key in Settings."
        }
        return error.localizedDescription
    }

    // MARK: - Custom transcription (file-pick) flow

    /// Public entry point invoked from the SwiftUI "Transcribe file…" button.
    /// Activates the app, presents an `NSOpenPanel`, and on user selection
    /// hands off to the async pipeline.
    @MainActor
    func transcribePickedFile() {
        // Bring the app to front so the modal sheet appears reliably above
        // the menubar popover (which is .transient and will dismiss).
        NSApp.activate(ignoringOtherApps: true)

        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        let exts = ["mp3", "m4a", "wav", "aac", "mp4", "mov", "m4v", "caf", "aiff", "flac"]
        let extTypes = exts.compactMap { UTType(filenameExtension: $0) }
        panel.allowedContentTypes = [.audio, .movie] + extTypes
        panel.title = "Choose audio or video to transcribe"
        panel.prompt = "Transcribe"

        guard panel.runModal() == .OK, let picked = panel.url else { return }

        // Decouple the panel cleanup from the async pipeline.
        Task { @MainActor [weak self] in
            await self?.handlePickedFile(picked)
        }
    }

    /// Drives transcode → upload for a picked audio/video file. Sets `mode`
    /// transitions and surfaces any failure as a notification.
    @MainActor
    func handlePickedFile(_ picked: URL) async {
        let stem = picked.deletingPathExtension().lastPathComponent

        mode = .converting

        let subdir: URL
        do {
            subdir = try CustomTranscriptionsStore.makeJobDirectory(forStem: stem)
        } catch {
            let message = formatTranscriptionError(error)
            Log.error("Custom transcription: makeJobDirectory failed: \(message)", category: "transcribe")
            AppNotifications.notify(title: "Custom Transcription Failed", body: message)
            mode = .idle
            return
        }

        // Copy the picked file as-is into the per-job folder. The server now
        // ffmpeg-decodes the original container; no client-side transcoding.
        let originalDestination = subdir.appendingPathComponent(picked.lastPathComponent)
        do {
            try FileManager.default.copyItem(at: picked, to: originalDestination)
        } catch {
            let message = formatTranscriptionError(error)
            Log.error("Custom transcription: copy source file failed: \(message)", category: "transcribe")
            AppNotifications.notify(title: "Custom Transcription Failed", body: message)
            try? FileManager.default.removeItem(at: subdir)
            mode = .idle
            return
        }

        await processCustomTranscriptionUpload(
            sourceURL: originalDestination,
            outputDirectory: subdir,
            stem: stem
        )
    }

    /// Uploads a custom-transcription source file (original container, no
    /// client-side transcoding) via `POST /transcribe/file`. Mirrors
    /// `processMeetingUpload` but skips the offline-queue path: custom
    /// transcriptions are always user-initiated and don't get retried later.
    @MainActor
    func processCustomTranscriptionUpload(
        sourceURL: URL,
        outputDirectory: URL,
        stem: String
    ) async {
        mode = .uploading
        recordingState.uploadFraction = 0

        do {
            try await runFileTranscriptionJob(
                sourceURL: sourceURL,
                outputDirectory: outputDirectory,
                stem: stem,
                mode: "file"
            )

            // Harmless even though custom transcripts live in subfolders the
            // store doesn't index — keeps consistency with the meeting path.
            TranscriptStore.shared.refresh()
            NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)

            AppNotifications.notify(title: "Custom Transcription Complete", body: stem)
            Log.info("Custom transcription complete — \(stem)", category: "transcribe")

            mode = .done
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            mode = .idle
        } catch {
            let message = formatTranscriptionError(error)
            Log.error("Custom transcription failed: \(message)", category: "transcribe")
            AppNotifications.notify(title: "Custom Transcription Failed", body: message)
            mode = .idle
        }
    }
}
