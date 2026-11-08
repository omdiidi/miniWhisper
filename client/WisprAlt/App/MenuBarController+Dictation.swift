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

    /// Stop → route → transcribe → inject. Filter OSLog timing with:
    /// `log show --last 5m --predicate 'subsystem == "co.wispralt" AND category == "dictation"' --info`
    @MainActor func runDictation(_ s: DictationSession, targetPID: pid_t?, fnReleasedAt: Date) async {
        let clock = ContinuousClock()
        let tStop = clock.now
        do {
            let out = try await dictationRecorder.stop()
            // Tasks 2-6 (no spool yet): the recording is deleted on every path.
            defer { try? FileManager.default.removeItem(at: out.recording.fileURL) }
            let stopMs = (clock.now - tStop).milliseconds

            let tNet = clock.now
            let result: DictationResult
            switch try s.route(local: out.local) {
            case .openAIFile(let provider): result = try await provider.finish(out.recording)
            case .local(let capture): result = try await LocalServerProvider().finish(capture: capture)
            }
            let netMs = (clock.now - tNet).milliseconds

            let tInj = clock.now
            await TextInjector.inject(result.text, targetPID: targetPID)
            Log.debug(
                "dictation/timing: stop_ms=\(String(format: "%.1f", stopMs)) net_ms=\(String(format: "%.1f", netMs)) " +
                "inject_ms=\(String(format: "%.1f", (clock.now - tInj).milliseconds))",
                category: "dictation"
            )
            Log.info(
                "Dictation done: chars=\(result.text.count) model=\(result.modelUsed) mode=\(s.snapshot.mode.rawValue)",
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
            let outcome = ErrorLog.outcome(error)
            if outcome == .failed(.modelNotFound), s.snapshot.mode == .online, let model = s.snapshot.model {
                // Reset the picker so the next dictation works.
                let fallback = OpenAIModels.defaultID(for: model.kind)
                Settings.shared.openAIModel = fallback
                showToast("That OpenAI model is not available. Switched to \(fallback).")
                return
            }
            if let body = Self.failureMessage(outcome, mode: s.snapshot.mode) {
                AppNotifications.notify(title: "Dictation Failed", body: body)
            }
        }
    }

    /// User-facing text for a failed dictation; nil means stay silent (an
    /// empty recording is an FN tap without speech).
    static func failureMessage(_ outcome: ErrorOutcome, mode: TranscriptionMode) -> String? {
        switch outcome {
        case .empty:
            return nil
        case .cancelled:
            return "The dictation was cancelled."
        case .failed(let kind):
            switch kind {
            case .keyMissing: return "Add your OpenAI key in Settings → Advanced."
            case .auth:
                return mode == .online
                    ? "OpenAI rejected the API key. Check it in Settings → Advanced."
                    : "API key rejected. Re-paste your API key in Settings."
            case .quota: return "OpenAI quota exceeded. Check your OpenAI billing."
            case .modelNotFound: return "That OpenAI model is not available. Pick another in Settings → Advanced."
            case .network: return "Network error. Check your connection and try again."
            case .server: return "The transcription service returned an error. Try again."
            case .deadline: return "Transcription took too long and was stopped."
            case .deviceChanged: return "Audio device changed — press FN again"
            case .writeFailed: return "Could not write audio to disk."
            case .encodeFailed: return "Could not encode the recording."
            case .other: return "Something went wrong. Try again."
            }
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
            ? settings.serverURL.map { ServerEndpoint(baseURL: $0, apiKey: (try? KeychainHelper.getAPIKey()) ?? nil) }
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

        let provider = ProviderFactory.make(snapshot)
        let started: Bool
        do {
            started = try dictationRecorder.start(dictationId: snapshot.id, endpoint: snapshot.streamEndpoint,
                                                  maxSeconds: snapshot.capSeconds)
        } catch {
            provider?.cancel()
            Log.error("DictationRecorder failed to start: \(ErrorLog.describe(error))", category: "dictation")
            return
        }
        guard started else {
            provider?.cancel()
            showToast("Still saving the last dictation — press again")
            return
        }
        currentSession = DictationSession(snapshot: snapshot, provider: provider)
        Log.info("Dictation started (mode=\(mode.rawValue)).", category: "dictation")
    }

    /// Called on the main actor by FNKeyMonitor when FN is released after a confirmed hold.
    @MainActor func dictationStop() {
        guard let s = currentSession else { return }
        beginRun(s)
    }
}
