import AppKit
import AVFoundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

private extension Duration {
    /// Convert a `Duration` (returned by `ContinuousClock` arithmetic) into
    /// floating-point milliseconds for log lines.
    var milliseconds: Double {
        let comps = self.components
        return Double(comps.seconds) * 1_000.0 + Double(comps.attoseconds) * 1e-15
    }
}

// MARK: - Dictation

extension MenuBarController {
    /// `DictationRecorder` posts `.dictationMaxDurationReached` once when the
    /// 10-minute cap is hit and drops further audio. Run the normal FN-up
    /// stop/submit path; the real FN-up that arrives later hits
    /// `dictationStop()`'s `mode == .dictating` guard and is a no-op.
    func configureDictationCapObserver() {
        dictationMaxDurationObserver = NotificationCenter.default.addObserver(
            forName: .dictationMaxDurationReached,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.mode == .dictating else { return }
                self.showToast("Dictation hit the 10-minute limit — sent")
                self.dictationStop()
            }
        }
    }

    /// Fires when the audio input device changes mid-dictation (AirPods plugged
    /// in/out, default input switched). The recorder has already invalidated
    /// its tap — we just need to reset the menubar state to idle and surface a
    /// brief notification so the user knows to retry.
    @objc func handleDictationConfigChanged() {
        Task { @MainActor in
            guard self.mode == .dictating else { return }
            Log.info(
                "MenuBarController: aborting in-flight dictation — audio device changed.",
                category: "dictation"
            )
            // Best-effort cleanup: stop the recorder if it's still running.
            // We discard whatever partial WAV exists; transcribing it would
            // fail anyway (engine state is invalid).
            Task.detached {
                _ = try? await self.dictationRecorder.stop()
            }
            self.mode = .idle
            AppNotifications.notify(
                title: "Dictation Cancelled",
                body: "Audio input device changed mid-recording. Press FN again to retry."
            )
        }
    }

    // MARK: - Mic mutual exclusion (v3 delta)

    /// Attempt to start dictation. Returns false and logs a warning toast if a meeting is active.
    @discardableResult
    func tryStartDictation() -> Bool {
        guard !isMeetingActive else {
            Log.warning(
                "Dictation start ignored — meeting recording is active.",
                category: "dictation"
            )
            showToast("Dictation unavailable while meeting recording is active.")
            return false
        }
        return true
    }
}

// MARK: - FNKeyEventsDelegate

extension MenuBarController: FNKeyEventsDelegate {

    /// Called on the main actor by FNKeyMonitor when FN has been held ≥ holdThreshold.
    @MainActor func dictationStart() {
        guard tryStartDictation() else { return }
        mode = .dictating
        do {
            try dictationRecorder.start()
            Log.info("Dictation started.", category: "dictation")
        } catch {
            mode = .idle
            Log.error("DictationRecorder failed to start: \(error.localizedDescription)", category: "dictation")
        }
    }

    /// Called on the main actor by FNKeyMonitor when FN is released after a confirmed hold.
    @MainActor func dictationStop() {
        guard mode == .dictating else { return }
        // Set idle immediately so the icon stops flashing.
        mode = .idle

        // FN-release wall-clock — captured BEFORE the network round-trip so
        // the latency ring buffer records "FN-up → paste-done" wall delta
        // (A/B timing). Wall-clock here is intentional — we're
        // measuring user-perceived latency, not monotonic intervals.
        let fnReleasedAt = Date()

        // Capture the user's intended target window NOW — before the network
        // round-trip (which can take 10-20s on slow uplinks) gives them time
        // to switch focus. TextInjector activates this PID before resolving
        // the AX element so dictation lands where they were when they
        // finished speaking, not where focus drifts during the upload.
        let targetPID: pid_t? = NSWorkspace.shared.frontmostApplication?.processIdentifier

        Task { @MainActor in
            // Latency breakdown timestamps — surfaces the ~3-5s multi-sentence
            // hunch by isolating network+upload+inference vs AX-inject. Filter
            // OSLog with: `log show --last 5m --predicate 'subsystem == "co.wispralt"
            // AND category == "dictation"' --style compact --info`
            //
            // ContinuousClock is monotonic (cannot go backwards on NTP correction);
            // Date() was wall-clock and could produce negative latencies during
            // background `timed` adjustments. Issued at Log.debug so the per-
            // dictation chatter is off-by-default; flip via OSLog profile when
            // measuring.
            let clock = ContinuousClock()
            let tStopStart = clock.now
            do {
                let wavData = try await dictationRecorder.stop()
                let stopMs = (clock.now - tStopStart).milliseconds
                Log.debug(
                    "dictation/timing: stop_ms=\(String(format: "%.1f", stopMs)) bytes=\(wavData.count)",
                    category: "dictation"
                )

                let tNet = clock.now
                let text = try await DictationAPI.transcribe(wavData, recorder: dictationRecorder)
                let netMs = (clock.now - tNet).milliseconds
                Log.debug(
                    "dictation/timing: net_total_ms=\(String(format: "%.1f", netMs)) chars=\(text.count)",
                    category: "dictation"
                )

                let tInj = clock.now
                await TextInjector.inject(text, targetPID: targetPID)
                let injMs = (clock.now - tInj).milliseconds
                let totalMs = (clock.now - tStopStart).milliseconds
                Log.debug(
                    "dictation/timing: inject_ms=\(String(format: "%.1f", injMs)) total_ms=\(String(format: "%.1f", totalMs))",
                    category: "dictation"
                )
                Log.info("Dictation injected: \"\(text.prefix(60))\"", category: "dictation")

                // A/B timing: record FN-release → paste-done.
                // dedupId is only set when the streaming path engaged (≥8 s
                // dictation with at least one chunk armed); otherwise nil so
                // can bucket streaming vs non-streaming.
                var dedupId: String? = nil
                if let s = dictationRecorder.streamSession {
                    dedupId = s.clientDedupId
                }
                dictationRecorder.appendLatencyReading(
                    DictationRecorder.LatencyReading(
                        fnRelease: fnReleasedAt,
                        paste: Date(),
                        dedupId: dedupId
                    )
                )

                // Successful dictation is one of the four PendingUploadsQueue
                // drain triggers. Detached + utility priority so it doesn't
                // contend with the next dictation if the user FN-holds again
                // immediately.
                Task.detached(priority: .utility) {
                    await PendingUploadsQueue.shared.drain()
                }
                // Plan A §A8: also drain cloud-fallback queue on online
                // dictation success — a successful online dictation proves
                // the origin is reachable, so this is the best moment to
                // flush any queued OpenRouter-served dictations.
                Task.detached(priority: .utility) {
                    await DictationFallbackQueue.shared.drain()
                }

            } catch ServerError.unauthorized {
                Log.error("Dictation failed — unauthorized. Re-paste API key in Settings.", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Failed",
                    body: "API key rejected. Re-paste your API key in Settings."
                )
            } catch DictationRecorder.DictationError.emptyRecording {
                // FN tapped without speaking, or mic returned silence.
                // Don't notify — would be noisy on accidental taps.
                Log.info("Dictation: empty recording (no audio captured).", category: "dictation")
            } catch DictationRecorder.DictationError.writeFailed(let underlying) {
                Log.error("Dictation failed — file write error: \(underlying)", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Failed",
                    body: "Could not write audio to disk: \(underlying.localizedDescription)"
                )
            } catch DictationRecorder.DictationError.meetingRecordingActive {
                Log.info("Dictation suppressed — meeting recording is active.", category: "dictation")
                AppNotifications.notify(
                    title: "Dictation Unavailable",
                    body: "A meeting is recording — release the meeting first."
                )
            } catch {
                // When the fallback path itself fails (mini AND Worker
                // unreachable, or Worker rate-limited / budget-exhausted),
                // surface a debounced "transcription temporarily unavailable"
                // toast instead of a noisy localized-description.
                if case ServerError.server = error {
                    showFallbackUnavailableToast()
                }
                Log.error("Dictation failed: \(error.localizedDescription)", category: "dictation")
                AppNotifications.notify(title: "Dictation Failed", body: error.localizedDescription)
            }
        }
    }

}
