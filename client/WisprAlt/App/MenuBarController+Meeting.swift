import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Meeting lifecycle

extension MenuBarController {
    /// Fires when the CoreAudio HAL default input device changes mid-meeting
    /// recording. SCStream emits no equivalent callback, so AudioDeviceListener
    /// in MeetingRecorder detects this and posts .meetingConfigChanged.
    ///
    /// We snapshot `lastOutputURL` BEFORE calling stop() because SCStream's
    /// `didStopWithError` may have already flipped `isActive` to false, causing
    /// stop() to throw `.notRunning`. The partial WAV is still on disk in that
    /// case and must be cleaned up.
    @objc func handleMeetingConfigChanged() {
        Task { @MainActor in
            guard self.mode == .meetingRecording else { return }
            Log.info(
                "MenuBarController: aborting in-flight meeting recording — audio input device changed.",
                category: "capture"
            )
            // Snapshot the URL before stop() so we can delete the partial WAV
            // even if SCStream's didStopWithError already flipped isActive=false.
            let partialURL = MeetingRecorder.shared.lastOutputURL

            do {
                _ = try await MeetingRecorder.shared.stop()
            } catch {
                Log.warning(
                    "Meeting config-change abort: stop() threw \(error)",
                    category: "capture"
                )
            }

            // Delete the partial WAV — an interrupted meeting is not a valid recording.
            // Containment check: only delete if the URL is inside the configured
            // meetings directory. This prevents UserDefaults poisoning (or any
            // future bug that lets a wrong URL leak into MeetingRecorder.lastOutputURL)
            // from turning this cleanup into a write-anywhere primitive.
            // Codex review caught this.
            if let url = partialURL {
                let meetingsDir = Settings.shared.meetingsPath.standardizedFileURL.path
                let target = url.standardizedFileURL.path
                if target.hasPrefix(meetingsDir + "/") {
                    try? FileManager.default.removeItem(at: url)
                } else {
                    Log.warning(
                        "Meeting config-change abort: refused to delete partial WAV at \(target) (outside \(meetingsDir))",
                        category: "capture"
                    )
                }
            }

            self.meetingActive = false
            self.mode = .idle

            AppNotifications.notify(
                title: "Meeting Cancelled",
                body: "Audio input device changed mid-recording. Triple-tap FN to start a new meeting."
            )
        }
    }

    @objc func handleMeetingMaxDurationReached() {
        guard MeetingRecorder.shared.isActive else { return }
        let capMin = Settings.shared.maxMeetingMinutes
        Log.info("Meeting max duration reached (\(capMin) min) — stopping and uploading.", category: "meeting")
        AppNotifications.notify(
            title: "Meeting Recording Stopped",
            body: "\(capMin)-minute cap reached. Uploading now."
        )
        // toggleMeetingRecording is @MainActor; this NotificationCenter callback
        // is nonisolated. Hop to the main actor before invoking.
        Task { @MainActor in
            self.toggleMeetingRecording()
        }
    }

    @objc func handleMeetingApproachingCap() {
        let capMin = Settings.shared.maxMeetingMinutes
        Log.info("Meeting approaching \(capMin)-minute cap (60 min elapsed).", category: "meeting")
        AppNotifications.notify(
            title: "Meeting Recording",
            body: "60 minutes elapsed; maximum recording length is \(capMin) minutes."
        )
    }

    // MARK: - Meeting recording control

    func startMeetingRecording() {
        let now = Date()
        let startName = humanReadableMeetingFilename(start: now, end: nil, in: Settings.shared.meetingsPath)
        let outputURL = Settings.shared.meetingsPath.appendingPathComponent(startName)
        self.meetingRecordingStart = now
        self.meetingStartFileURL = outputURL

        Task { @MainActor in
            do {
                try await MeetingRecorder.shared.start(to: outputURL)
                meetingActive = true
                mode = .meetingRecording
                Log.info("Meeting recording started → \(startName)", category: "meeting")
            } catch {
                Log.error("Failed to start meeting recording: \(error.localizedDescription)", category: "meeting")
                AppNotifications.notify(title: "Meeting Recording Failed", body: error.localizedDescription)
            }
        }
    }

    func stopMeetingRecording() {
        Task { @MainActor in
            do {
                let wavURL = try await MeetingRecorder.shared.stop()
                let endDate = Date()
                let humanName = humanReadableMeetingFilename(
                    start: meetingRecordingStart ?? endDate,
                    end: endDate,
                    in: Settings.shared.meetingsPath
                )
                let renamedURL = Settings.shared.meetingsPath.appendingPathComponent(humanName)
                let finalURL: URL
                do {
                    try FileManager.default.moveItem(at: wavURL, to: renamedURL)
                    finalURL = renamedURL
                    Log.info("Meeting WAV renamed → \(humanName)", category: "meeting")
                } catch {
                    Log.warning("Could not rename meeting WAV: \(error.localizedDescription). Using start-only name.", category: "meeting")
                    finalURL = wavURL
                }
                meetingActive = false
                mode = .uploading
                recordingState.uploadFraction = 0
                Log.info("Meeting recording stopped — uploading \(finalURL.lastPathComponent)", category: "meeting")

                await processMeetingUpload(wavURL: finalURL)
            } catch {
                meetingActive = false
                mode = .idle
                Log.error("Failed to stop meeting recording: \(error.localizedDescription)", category: "meeting")
                AppNotifications.notify(title: "Meeting Recording Error", body: error.localizedDescription)
            }
        }
    }

    /// Uploads, polls, downloads, and finalises a completed meeting m4a.
    ///
    /// Post-Task-10: meetings are now AAC m4a containers and route through
    /// `POST /transcribe/file` via `runFileTranscriptionJob`. The legacy
    /// `/transcribe/meeting` POST is reserved for `PendingUploadsQueue`'s
    /// dual-replay of any pre-Task-9 `.wav` entries that are still queued
    /// from before the upgrade.
    @MainActor
    func processMeetingUpload(wavURL: URL) async {
        let baseName = wavURL.deletingPathExtension().lastPathComponent

        // Record the upload-attempt start so the offline-signature classifier
        // can compute elapsed time on the failure path. Used only when the
        // catch branch fires.
        let uploadStartedAt = Date()

        do {
            try await runFileTranscriptionJob(
                sourceURL: wavURL,
                outputDirectory: Settings.shared.meetingsPath,
                stem: baseName,
                mode: "meeting"
            )

            TranscriptStore.shared.refresh()
            NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)

            AppNotifications.notify(title: "Meeting transcribed", body: baseName)
            Log.info("Meeting transcription complete — \(baseName)", category: "meeting")

            mode = .done
            try await Task.sleep(nanoseconds: 3_000_000_000)
            mode = .idle

        } catch {
            mode = .idle

            // Offline-signature check: when the mini is unreachable we queue
            // the recording locally for later retry rather than surfacing a
            // generic "transcription failed" error. Diarization can't run on
            // the cloud fallback, so meetings never proxy to OpenRouter.
            let attempt = Self.buildMeetingAttempt(error: error, startedAt: uploadStartedAt)
            if ServerClient.shared.isOfflineSignature(attempt) {
                do {
                    try PendingUploadsQueue.shared.enqueue(wav: wavURL)
                    showOfflineMeetingToast()
                    refreshPendingMenu()
                    Log.warning(
                        "Meeting upload offline-signature confirmed — queued \(baseName) locally.",
                        category: "fallback"
                    )
                } catch {
                    Log.error(
                        "Meeting offline AND queue.enqueue failed: \(error.localizedDescription)",
                        category: "fallback"
                    )
                    AppNotifications.notify(
                        title: "Meeting Save Failed",
                        body: "Server offline and could not save recording locally."
                    )
                }
                return
            }

            let message = formatTranscriptionError(error)
            Log.error("Meeting processing failed: \(message)", category: "meeting")
            AppNotifications.notify(title: "Meeting Transcription Failed", body: message)
        }
    }

    /// Build a `ServerClient.RequestAttempt` from a thrown meeting-upload
    /// error so the offline-signature classifier can run on the catch path.
    /// `MeetingAPI.submit` builds its own URLSession, so it doesn't go
    /// through `ServerClient.execute` — we synthesize the attempt here.
    static func buildMeetingAttempt(
        error: Error,
        startedAt: Date
    ) -> ServerClient.RequestAttempt {
        let finishedAt = Date()
        let outcome: ServerClient.RequestAttempt.Outcome
        if let urlErr = error as? URLError {
            outcome = .error(urlErr)
        } else if case ServerError.transport(let underlying) = error {
            outcome = .error(underlying)
        } else if case ServerError.server(let status, _) = error,
                  let url = Settings.shared.serverURL,
                  let synthetic = HTTPURLResponse(
                      url: url,
                      statusCode: status,
                      httpVersion: "HTTP/1.1",
                      headerFields: ["X-Request-Id": "synthetic"]
                  )
        {
            // Synthetic origin response — has X-Request-Id, so classifier
            // refuses to fall back. That's correct: an origin 4xx/5xx is
            // not a tunnel-level failure.
            outcome = .response(synthetic)
        } else {
            outcome = .error(error)
        }
        return ServerClient.RequestAttempt(
            startedAt: startedAt,
            finishedAt: finishedAt,
            lastByteSentAt: nil,
            outcome: outcome
        )
    }

    // MARK: - Human-readable meeting filename

    func humanReadableMeetingFilename(start: Date, end: Date?, in dir: URL) -> String {
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.dateFormat = "EEE MMM d"

        let timeFormatter = DateFormatter()
        timeFormatter.locale = Locale(identifier: "en_US_POSIX")
        timeFormatter.amSymbol = "am"
        timeFormatter.pmSymbol = "pm"
        // Periods, not colons (filesystem-friendly across rsync to Linux, zip, etc.)
        // No seconds — user wants the readable form "3.05-5.20pm".
        // Collision is handled below by appending " (2)" / " (3)" if needed.
        timeFormatter.dateFormat = "h.mma"

        let day = dayFormatter.string(from: start)
        let startTime = timeFormatter.string(from: start)
        let base: String
        if let end = end {
            let endTime = timeFormatter.string(from: end)
            base = "\(day) \(startTime)-\(endTime)"
        } else {
            base = "\(day) \(startTime)"
        }

        // Collision guard: check the base name against ALL sidecar extensions.
        // `wav` is kept in the collision list (alongside `m4a`) so a fresh m4a
        // recording does not stomp legacy WAV meetings sharing the same minute.
        let exts = ["m4a", "wav", "json", "srt", "vtt", "txt"]
        func anyExists(_ baseName: String) -> Bool {
            for ext in exts {
                if FileManager.default.fileExists(atPath: dir.appendingPathComponent("\(baseName).\(ext)").path) {
                    return true
                }
            }
            return false
        }
        var name = base
        var i = 2
        while anyExists(name) {
            name = "\(base) (\(i))"
            i += 1
        }
        return "\(name).m4a"
    }

    /// Called on the main actor by FNKeyMonitor on triple-tap.
    @MainActor func toggleMeetingRecording() {
        if MeetingRecorder.shared.isActive {
            stopMeetingRecording()
        } else {
            startMeetingRecording()
        }
    }
}

// MARK: - Private error types

enum MeetingProcessingError: Error, LocalizedError {
    case serverFailed(String)
    case pollTimedOut

    var errorDescription: String? {
        switch self {
        case .serverFailed(let reason):
            return "Server-side processing failed: \(reason)"
        case .pollTimedOut:
            return "Server didn't respond in time; check /metrics for job status."
        }
    }
}
