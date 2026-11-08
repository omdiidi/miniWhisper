import AppKit
import Foundation
import WisprAltCore

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
    /// dictation cap is hit and drops further audio. Run the normal FN-up
    /// path; the real FN-up that arrives later finds no `currentSession`.
    func configureDictationCapObserver() {
        dictationMaxDurationObserver = NotificationCenter.default.addObserver(
            forName: .dictationMaxDurationReached,
            object: nil,
            queue: .main
        ) { [weak self] note in
            let seconds = note.userInfo?["seconds"] as? Int ?? OpenAIModels.maxDictationSeconds
            Task { @MainActor [weak self] in
                guard let self, let s = self.currentSession else { return }
                self.showToast("Dictation hit the \(Self.capLabel(seconds)) limit — sent")
                self.beginRun(s)
            }
        }
    }

    /// "10-minute", or "90-second" for a cap that is not whole minutes.
    static func capLabel(_ seconds: Int) -> String {
        seconds % 60 == 0 ? "\(seconds / 60)-minute" : "\(seconds)-second"
    }

    /// Fires when the audio input device changes mid-dictation (AirPods plugged
    /// in/out, default input switched). The recorder has already invalidated
    /// its tap; run the normal stop path, whose `stop()` reports the device
    /// change (outcome `.deviceChanged` → "press FN again" toast).
    @objc func handleDictationConfigChanged() {
        Task { @MainActor in
            guard let s = self.currentSession else { return }
            Log.info("MenuBarController: audio device changed mid-dictation.", category: "dictation")
            self.beginRun(s)
        }
    }

    /// FN-up / cap / device change: end the session in this main-actor turn
    /// (so `isBusy` never dips) and transcribe in a Task.
    @MainActor func beginRun(_ s: DictationSession) {
        dictationsInFlight += 1
        currentSession = nil
        // FN-release wall-clock, captured BEFORE the network round-trip, so the
        // latency ring records "FN-up → paste-done" as the user perceives it.
        let fnReleasedAt = Date()
        // Capture the user's intended target window NOW — before the network
        // round-trip gives them time to switch focus. TextInjector activates
        // this PID before resolving the AX element.
        let targetPID: pid_t? = NSWorkspace.shared.frontmostApplication?.processIdentifier
        Task { @MainActor in
            defer { dictationsInFlight -= 1 }
            await runDictation(s, targetPID: targetPID, fnReleasedAt: fnReleasedAt)
        }
    }

    /// Stop → route → transcribe → Mercury (opt-in) → last text → inject → spool disposition.
    /// Filter OSLog timing with:
    /// `log show --last 5m --predicate 'subsystem == "co.wispralt" AND category == "dictation"' --info`
    @MainActor func runDictation(_ s: DictationSession, targetPID: pid_t?, fnReleasedAt: Date) async {
        let clock = ContinuousClock()
        let tStop = clock.now
        var outcome: DictationOutcome
        var delivery: InjectOutcome? = nil
        var interrupted = false
        do {
            // The spool file is owned by the disposition below, on every path.
            let out = try await dictationRecorder.stop()
            interrupted = out.recording.interruptedByDeviceChange
            let stopMs = (clock.now - tStop).milliseconds

            let tNet = clock.now
            var result: DictationResult
            switch try s.route(local: out.local) {
            case .openAIFile(let provider), .openAIRealtime(let provider):
                result = try await provider.finish(out.recording)
            case .local(let capture, let endpoint):
                result = try await LocalServerProvider(endpoint: endpoint, dictationId: s.snapshot.id)
                    .finish(capture: capture, durationSeconds: out.recording.durationSeconds)
            }
            let netMs = (clock.now - tNet).milliseconds
            let cut = TruncationCheck.isTruncated(outputTokens: result.outputTokens,
                                                  verifiedCap: s.snapshot.model?.maxOutputTokens)
            result = await MercuryStep.apply(result)
            recordingState.lastFormatting = result.formatting

            if result.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                outcome = .emptyText
            } else {
                LastTextStore.save(result, mode: s.snapshot.mode)   // recoverable before any paste
                let tInj = clock.now
                delivery = await TextInjector.inject(result.text, targetPID: targetPID)
                outcome = .text(truncated: cut, serverSaved: result.serverSaved)
                Log.debug(
                    "dictation/timing: stop_ms=\(String(format: "%.1f", stopMs)) net_ms=\(String(format: "%.1f", netMs)) " +
                    "inject_ms=\(String(format: "%.1f", (clock.now - tInj).milliseconds))",
                    category: "dictation"
                )
            }
            Log.info(
                "Dictation done: chars=\(result.text.count) model=\(result.modelUsed) mode=\(s.snapshot.mode.rawValue) " +
                "saved=\(result.serverSaved) truncated=\(cut) formatting=\(LastTranscriptCaption.formattingLabel(result.formatting))",
                category: "dictation"
            )

            // A/B timing: FN-release → paste-done. The dedup id is set only
            // when the Local streaming session was armed at start.
            dictationRecorder.appendLatencyReading(
                DictationRecorder.LatencyReading(
                    fnRelease: fnReleasedAt,
                    paste: Date(),
                    dedupId: out.local?.session?.clientDedupId
                )
            )
        } catch {
            s.provider?.cancel()
            Log.error("dictation failed: \(ErrorLog.describe(error))", category: "dictation")
            outcome = DictationOutcome(ErrorLog.outcome(error))
            if outcome == .failed(.modelNotFound), s.snapshot.mode == .online {
                // Reset the picker to the known-good file model so the next dictation works.
                Settings.shared.openAIModel = OpenAIModels.defaultFileModelID
            }
        }
        if interrupted { showToast("Audio device changed — transcribed what was recorded") }
        applySpool(SpoolDisposition.decide(outcome, delivery: delivery), id: s.snapshot.id, mode: s.snapshot.mode)
    }

    /// User-facing text for a failed dictation (an empty recording is silent
    /// and never reaches here).
    static func failureMessage(_ kind: FailureKind, mode: TranscriptionMode) -> String {
        switch kind {
        case .keyMissing: return "Add your OpenAI key in Settings → Advanced."
        case .auth:
            return mode == .online
                ? "OpenAI rejected the API key. Check it in Settings → Advanced."
                : "API key rejected. Re-paste your API key in Settings."
        case .quota: return "OpenAI quota exceeded. Check your OpenAI billing."
        case .modelNotFound:
            return "That OpenAI model is not available. Switched to \(OpenAIModels.defaultFileModelID)."
        case .network: return "Network error. Check your connection and try again."
        case .server: return "The transcription service returned an error. Try again."
        case .deadline: return "Transcription took too long and was stopped."
        case .deviceChanged: return "Audio device changed — press FN again"
        case .writeFailed: return "Could not write audio to disk."
        case .encodeFailed: return "Could not encode the recording."
        case .other: return "Something went wrong. Try again."
        case .rejected: return "The server refused this dictation (deleted or not yours). It will not be retried."
        }
    }
}

// MARK: - FNKeyEventsDelegate

extension MenuBarController: FNKeyEventsDelegate {

    /// Called on the main actor by FNKeyMonitor when FN has been held ≥ holdThreshold.
    /// Everything here is synchronous, so FN-up cannot run in between.
    @MainActor func dictationStart() {
        guard currentSession == nil else { return }
        let settings = Settings.shared
        let mode = settings.mode
        // Online reads only the OpenAI key; Local reads only the Local server.
        let endpoint: ServerEndpoint? = mode == .local
            ? try? ServerClient.shared.currentEndpoint()
            : nil
        let snapshot = DictationSnapshot(
            mode: mode,
            model: OpenAIModels.resolve(id: settings.openAIModel),
            keyPresent: mode == .online && OpenAIKey.current() != nil,
            endpoint: endpoint
        )
        if snapshot.kind == .needKey {
            showToast("Add your OpenAI key to dictate in Online mode.")
            openKeyEntry()
            return
        }

        // The sidecar lands (synchronously) before the spool file opens, so
        // any audio on disk always knows its mode and model.
        do {
            try SpoolFiles.writeSidecarSync(id: snapshot.id,
                                            SpoolSidecar(mode: mode, model: snapshot.model?.id ?? "local"))
        } catch {
            Log.error("spool sidecar write failed: \(ErrorLog.describe(error))", category: "dictation")
            showToast("Could not save audio")
            return
        }

        let provider = ProviderFactory.make(snapshot)
        let liveSink = provider as? LiveFrameSink
        let started: Bool
        do {
            started = try dictationRecorder.start(dictationId: snapshot.id, endpoint: snapshot.streamEndpoint,
                                                  maxSeconds: snapshot.capSeconds, frameSink: liveSink)
        } catch {
            provider?.cancel()
            SpoolFiles.removeSidecarSync(snapshot.id)
            Log.error("DictationRecorder failed to start: \(ErrorLog.describe(error))", category: "dictation")
            return
        }
        guard started else {
            provider?.cancel()
            SpoolFiles.removeSidecarSync(snapshot.id)
            showToast("Still saving the last dictation — press again")
            return
        }
        liveSink?.start()   // opens the Realtime socket; frames stream while FN is held
        currentSession = DictationSession(snapshot: snapshot, provider: provider)
        Log.info("Dictation started (mode=\(mode.rawValue)).", category: "dictation")
    }

    /// Called on the main actor by FNKeyMonitor when FN is released after a confirmed hold.
    @MainActor func dictationStop() {
        guard let s = currentSession else { return }
        beginRun(s)
    }
}
