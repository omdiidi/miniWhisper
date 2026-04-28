import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation
import os.lock

extension Notification.Name {
    /// Posted when AVAudioEngine's input configuration changes mid-recording
    /// (e.g. user plugs in / unplugs AirPods, switches default input device).
    /// MenuBarController observes this to abort the in-flight dictation and
    /// return the UI to idle — the engine has already invalidated the tap by
    /// the time this fires.
    static let dictationConfigChanged = Notification.Name("co.wispralt.dictationConfigChanged")
}

// MARK: - DictationRecorder

/// Records microphone audio via `AVAudioEngine` and produces a native-rate
/// **Float32 PCM** WAV (typically 48 kHz, native channel count) suitable for
/// the `/transcribe/dictate` endpoint. Server (`audio.py`) handles the
/// resample to 16 kHz and the multi-channel → mono downmix via `np.mean`.
///
/// ## Why Float32 + native rate (zero client conversion)
/// Two prior approaches both failed:
/// 1. `AVAudioConverter` (downmix to 16 kHz mono Float32): default channel-mix
///    sums channels without averaging, producing out-of-range floats with
///    peak ≈ 3.97. Server returned `duration_ms=0`.
/// 2. `AVAudioFile.write` to Int16 PCM (let AVAudioFile convert Float→Int16):
///    AVAudioFile's internal converter applies a buggy normalization that
///    amplifies the signal ~140x. A clean 0.24-peak voice arrives at the file
///    as a 32750/32767 (rail-clipped) Int16, destroying speech intelligibility
///    for Parakeet (it returned random one-word hallucinations).
///
/// The fix: write Float32 PCM at native sample rate, format-matched to the
/// tap's buffers. AVAudioFile.write performs zero conversion — it streams the
/// float bytes to disk byte-for-byte. The server reads via soundfile + librosa
/// which handle Float32 WAVs natively.
///
/// ## Concurrency
/// - The `installTap` callback fires on AVAudioEngine's realtime render
///   thread. We dispatch each `AVAudioFile.write` onto a serial `ioQueue`
///   so disk I/O never blocks the render thread.
/// - `stop()` sets a `stopRequested` fence so the tap drops in-flight
///   buffers, removes the tap, stops the engine, then `ioQueue.sync`-drains
///   pending writes before releasing the file. This closes the TOCTOU race
///   between engine teardown and the writer.
/// - `frameCounter` and `writeError` are guarded by `OSAllocatedUnfairLock`
///   for safe access from the tap callback and `stop()`.
///
/// ## Mic mutual exclusion
/// `start()` returns `false` if `MeetingRecorder.shared.isActive` is `true`,
/// because both recorders cannot share the input node simultaneously. The
/// caller should present a brief UI toast explaining the no-op.
///
/// ## Lifetime
/// Typical usage: create once; call `start()` on FN key-down, `stop()` on
/// FN key-up. `stop()` is `async` so the file read off disk doesn't block
/// the caller's actor.
final class DictationRecorder {

    // MARK: - Errors

    enum DictationError: Error, LocalizedError {
        case meetingRecordingActive
        case engineStartFailed(Error)
        case notRecording
        case writeFailed(Error)
        case emptyRecording

        var errorDescription: String? {
            switch self {
            case .meetingRecordingActive:
                return "Cannot start dictation while meeting recording is active."
            case .engineStartFailed(let err):
                return "AVAudioEngine failed to start: \(err.localizedDescription)"
            case .notRecording:
                return "DictationRecorder is not currently recording."
            case .writeFailed(let err):
                return "Audio file write failed: \(err.localizedDescription)"
            case .emptyRecording:
                return "Recording was empty — no audio captured."
            }
        }
    }

    // MARK: - State

    private let engine = AVAudioEngine()

    /// Serial queue used to perform AVAudioFile.write off the realtime render thread.
    /// AVAudioFile is not safe to call from the render thread because write may block
    /// on disk I/O and Float→Int16 conversion. We dispatch each tap buffer onto this
    /// queue, preserving order, and only synchronize on stop().
    private let ioQueue = DispatchQueue(label: "co.wispralt.dictation.io", qos: .userInitiated)

    /// AVAudioFile for the in-progress recording (overwritten each call to start()).
    /// Only mutated on `ioQueue` (and on the main thread before start / after fence).
    private var audioFile: AVAudioFile?

    /// Path to the in-progress WAV file.
    private var audioFileURL: URL?

    /// Native input sample rate (used for diagnostics).
    private var inputSampleRate: Double = 0

    /// Native input channel count, captured at start() for diagnostic logs.
    /// We do NOT downmix client-side — the server averages channels via np.mean.
    private var inputChannelCount: AVAudioChannelCount = 1

    /// Accumulated frame count, mutated on `ioQueue`.
    /// Diagnostic only — read after the queue has been drained on stop.
    private let frameCounter = OSAllocatedUnfairLock<AVAudioFrameCount>(initialState: 0)

    /// Set when the tap encounters a write error; surfaced from `stop()`.
    private let writeError = OSAllocatedUnfairLock<Error?>(initialState: nil)

    /// Set to true when stop() begins; the tap callback drops buffers after this
    /// to close the TOCTOU race between `engine.stop()` and `audioFile = nil`.
    private let stopRequested = OSAllocatedUnfairLock<Bool>(initialState: false)

    /// Maximum absolute Float32 sample value seen across the recording, captured
    /// pre-write so we can distinguish "mic delivered out-of-range floats" from
    /// "AVAudioFile clipped on Int16 conversion". Logged in stop().
    private let floatPeak = OSAllocatedUnfairLock<Float>(initialState: 0)

    private var isRecording = false

    /// Observer token for AVAudioEngineConfigurationChange notifications.
    private var configChangeObserver: NSObjectProtocol?

    /// True while a programmatic device override is settling. macOS fires
    /// `AVAudioEngineConfigurationChange` asynchronously after we set
    /// `kAudioOutputUnitProperty_CurrentDevice`, so the configChange observer
    /// would catch OUR OWN override and abort the recording. We swallow the
    /// first such notification per recording session.
    private var pendingDeviceOverride: Bool = false

    /// Mach time (seconds) when the current session's recording started. Used
    /// to ignore configChange notifications that arrive within a small settling
    /// window after start() — these are almost always delayed AVAudioEngine
    /// callbacks from a previous session's device override that landed late.
    ///
    /// The window is 100 ms — long enough to cover the typical synthetic-callback
    /// delay (observed 10–50 ms in practice), short enough that a user-initiated
    /// device change in the first 100 ms is vanishingly unlikely (a human can't
    /// hold FN, start dictating, AND swap mics that fast). Real device changes
    /// after the window still abort as before.
    ///
    /// Known limitation: notifications are dispatched on `.main`, so under heavy
    /// main-thread pressure delivery latency can exceed 100 ms even for
    /// in-session synthetic callbacks. In that case a real device change in
    /// the first 100 ms WILL be ignored. We accept this because the alternative
    /// (per-session AVAudioEngine reinstantiation, the only way to distinguish
    /// stale-cross-session callbacks from in-session ones) carries a much
    /// higher cost (audio graph rebuild on every FN press). The settling
    /// window's worst-case false-negative is one missed mid-recording device
    /// abort; the worst-case false-positive (no settling window) is every
    /// recording randomly aborting on its second activation.
    private var sessionStartTime: TimeInterval = 0
    private static let configChangeSettleWindow: TimeInterval = 0.1

    // MARK: - Public API

    /// Starts microphone capture.
    ///
    /// - Returns: `false` with an info log if meeting recording is active (mic exclusion).
    ///   `true` on success.
    /// - Throws: `DictationError.engineStartFailed` if the audio engine cannot start.
    @discardableResult
    func start() throws -> Bool {
        // Mic mutual exclusion: no-op when meeting recording is in progress.
        if MeetingRecorder.shared.isActive {
            Log.info("DictationRecorder: start() suppressed — MeetingRecorder is active.", category: "capture")
            return false
        }

        guard !isRecording else { return true }

        // Always start with a clean swallow flag. If a previous session armed it
        // and the synthetic configChange never fired (or fired before the observer
        // was installed), the flag would otherwise leak into THIS session and
        // swallow a real device-change event mid-recording. Clear it before any
        // device-override logic runs below.
        pendingDeviceOverride = false

        // Stamp session start so the observer can ignore configChange notifications
        // that arrive within the settling window — those are nearly always late
        // callbacks from a PREVIOUS session's device override that landed after
        // stop() returned. Without this, a delayed notification could abort the
        // current session as if a real mid-recording device change happened.
        sessionStartTime = Date().timeIntervalSinceReferenceDate

        let inputNode = engine.inputNode

        // === Apply preferred input device BEFORE format read & observer install.
        // The format read MUST happen on the new device — switching device changes
        // sample rate / channel count. The configChange observer installed below
        // would also catch our override and abort the recording, so we do this
        // before the observer is wired up. ===
        if let preferredUID = Settings.shared.preferredInputDeviceUID,
           let deviceID = MicEnumerator.audioDeviceID(forUID: preferredUID),
           let audioUnit = inputNode.audioUnit {
            var devID = deviceID
            let status = AudioUnitSetProperty(
                audioUnit,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                0,
                &devID,
                UInt32(MemoryLayout<AudioDeviceID>.size)
            )
            if status != noErr {
                Log.warning(
                    "Could not set preferred input device on inputNode: \(status). Falling back to system default.",
                    category: "audio"
                )
            } else {
                // macOS fires AVAudioEngineConfigurationChange async on the main
                // queue after this property change settles. The observer below
                // would catch it and abort the recording. Set the swallow flag
                // BEFORE the observer is installed.
                pendingDeviceOverride = true
                Log.info(
                    "DictationRecorder: input device set to UID \(preferredUID); next configChange will be swallowed.",
                    category: "audio"
                )
            }
        } else if let preferredUID = Settings.shared.preferredInputDeviceUID {
            // UID resolved to nil — device unplugged since last selection.
            Log.warning(
                "DictationRecorder: preferred device UID \(preferredUID) is unavailable; using system default.",
                category: "audio"
            )
        }

        let inputFormat = inputNode.outputFormat(forBus: 0)

        // Defensive: a headless macOS / disabled mic / not-yet-configured input
        // can yield sampleRate==0 or channelCount==0. Tap installation would
        // succeed silently and we'd produce a 0-byte WAV.
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw DictationError.engineStartFailed(
                NSError(domain: "co.wispralt", code: -10,
                        userInfo: [NSLocalizedDescriptionKey:
                            "Invalid input format: \(inputFormat.sampleRate)Hz \(inputFormat.channelCount)ch. " +
                            "Microphone may be unavailable or permission revoked."])
            )
        }

        inputSampleRate = inputFormat.sampleRate
        inputChannelCount = inputFormat.channelCount
        frameCounter.withLock { $0 = 0 }
        writeError.withLock { $0 = nil }
        stopRequested.withLock { $0 = false }
        floatPeak.withLock { $0 = 0 }

        // Write directly in the input format. The server (soundfile + librosa)
        // resamples to 16 kHz internally, so we don't need to do it client-side.
        // Avoiding AVAudioConverter eliminates a class of conversion bugs that
        // produced out-of-range float values (peak >> 1.0).
        //
        // Channel count: we keep the native channel count rather than forcing
        // mono. Mono downmix happens server-side via `np.mean(axis=1)` in
        // `wispralt_server/audio.py`, which is the mathematically correct
        // average. AVAudioConverter's default channel-mix on macOS sums
        // channels without averaging — that bug is exactly what produced the
        // peak=3.974 floats we just removed.
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("wispralt_dictation_\(UUID().uuidString).wav")
        audioFileURL = tempURL

        // Write a Float32 WAV that EXACTLY matches the tap's native format
        // (sampleRate, channelCount, Float32, deinterleaved). Matching the
        // buffer format means AVAudioFile.write performs zero conversion —
        // it just streams the float bytes to disk. Empirically, asking
        // AVAudioFile to convert Float32 → Int16 inline applies a buggy
        // internal normalization that amplifies the signal ~140x and rails
        // out the Int16, destroying speech intelligibility for Parakeet.
        //
        // The server's audio.py reads Float32 WAVs via soundfile + librosa
        // and resamples to 16 kHz cleanly.
        let outSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: inputFormat.sampleRate,
            AVNumberOfChannelsKey: inputFormat.channelCount,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: true
        ]

        do {
            let file = try AVAudioFile(
                forWriting: tempURL,
                settings: outSettings,
                commonFormat: .pcmFormatFloat32,
                interleaved: false
            )
            audioFile = file
        } catch {
            Log.error("DictationRecorder: failed to open AVAudioFile: \(error)", category: "capture")
            audioFileURL = nil
            try? FileManager.default.removeItem(at: tempURL)
            throw DictationError.engineStartFailed(error)
        }

        // Subscribe to engine configuration changes (e.g. AirPods plugged in
        // mid-recording) so we can fail loudly instead of producing a corrupt
        // file. AVAudioEngine auto-uninstalls the tap on these events.
        configChangeObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            // Swallow the FIRST configChange after a programmatic device
            // override — it's our own AudioUnitSetProperty settling, not a
            // real device-change event. Subsequent notifications still abort
            // (mid-recording AirPods plug, etc.).
            if self.pendingDeviceOverride {
                self.pendingDeviceOverride = false
                Log.info(
                    "DictationRecorder: swallowed configChange from programmatic device override.",
                    category: "capture"
                )
                return
            }
            // Settling window: ignore configChange notifications that arrive
            // within `configChangeSettleWindow` of session start. Late callbacks
            // from a previous session's device override sometimes land here
            // even after stop() — without this guard they'd abort the new
            // session as if a real mid-recording device change happened.
            let elapsed = Date().timeIntervalSinceReferenceDate - self.sessionStartTime
            if elapsed < Self.configChangeSettleWindow {
                Log.info(
                    "DictationRecorder: ignored configChange within settling window (\(Int(elapsed * 1000))ms after start).",
                    category: "capture"
                )
                return
            }
            Log.error(
                "DictationRecorder: AVAudioEngineConfigurationChange fired — " +
                "tap uninstalled. Aborting recording.",
                category: "capture"
            )
            self.writeError.withLock { existing in
                if existing == nil {
                    existing = DictationError.engineStartFailed(
                        NSError(domain: "co.wispralt", code: -11,
                                userInfo: [NSLocalizedDescriptionKey:
                                    "Audio device configuration changed mid-recording."])
                    )
                }
            }
            // Notify the UI layer so the menubar state machine can abort the
            // recording session — without this, the menubar shows "recording"
            // forever (FN release won't fire stop() because the engine has
            // already invalidated the tap).
            NotificationCenter.default.post(name: .dictationConfigChanged, object: nil)
        }

        // Install tap in input format. AVAudioFile.write performs the format
        // conversion (Float32 → Int16) for us.
        //
        // bufferSize 4096 frames ≈ 85ms @ 48kHz, ≈ 93ms @ 44.1kHz — large enough
        // to amortize file-system writes, small enough that stop() doesn't
        // block on a final flush past one buffer's worth of data.
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            // Stop fence: drop any buffers that race in after stop() began.
            if self.stopRequested.withLock({ $0 }) { return }

            // Defensive: macOS input chains (especially with Voice Isolation /
            // Wide Spectrum mic mode) sometimes deliver Float32 samples with
            // magnitude > 1.0. AVAudioFile's internal Float→Int16 conversion
            // hard-clips at the rails, which destroys speech intelligibility
            // for downstream ASR. We clamp in-place to ±0.95 to guarantee
            // headroom and track the pre-clamp peak for diagnostics.
            if let chans = buffer.floatChannelData {
                let frames = Int(buffer.frameLength)
                let channels = Int(buffer.format.channelCount)
                var localPeak: Float = 0
                for ch in 0..<channels {
                    let p = chans[ch]
                    for i in 0..<frames {
                        let v = p[i]
                        let mag = abs(v)
                        if mag > localPeak { localPeak = mag }
                        if v > 0.95 { p[i] = 0.95 }
                        else if v < -0.95 { p[i] = -0.95 }
                    }
                }
                self.floatPeak.withLock { existing in
                    if localPeak > existing { existing = localPeak }
                }
            }

            // Move the write off the realtime render thread onto our serial
            // I/O queue. AVAudioFile.write may block on disk I/O and Float→Int16
            // conversion; doing it inline can cause render-thread overruns.
            self.ioQueue.async { [weak self] in
                guard let self, let file = self.audioFile else { return }
                if self.stopRequested.withLock({ $0 }) { return }
                do {
                    try file.write(from: buffer)
                    self.frameCounter.withLock { $0 &+= buffer.frameLength }
                } catch {
                    Log.error("DictationRecorder: write failed: \(error)", category: "capture")
                    self.writeError.withLock { existing in
                        if existing == nil { existing = error }
                    }
                }
            }
        }

        // prepare() warms the audio graph so engine.start() doesn't pay
        // first-buffer latency on cold start (Apple-recommended).
        engine.prepare()

        do {
            try engine.start()
        } catch {
            // Full cleanup on engine failure: tap, observer, file, temp WAV.
            inputNode.removeTap(onBus: 0)
            if let token = configChangeObserver {
                NotificationCenter.default.removeObserver(token)
                configChangeObserver = nil
            }
            audioFile = nil
            audioFileURL = nil
            try? FileManager.default.removeItem(at: tempURL)
            throw DictationError.engineStartFailed(error)
        }

        isRecording = true
        Log.info(
            "DictationRecorder: recording started (input: \(Int(inputFormat.sampleRate))Hz \(inputFormat.channelCount)ch).",
            category: "capture"
        )
        return true
    }

    /// Stops capture and returns the encoded WAV bytes.
    ///
    /// Output WAV format: native sample rate (typically 48 kHz on macOS),
    /// native channel count, **Float32** PCM (non-interleaved). Server's
    /// `audio.py` resamples to 16 kHz and downmixes to mono via
    /// `np.mean(axis=1)`. See class doc-comment for the full rationale on
    /// why we write Float32 instead of Int16.
    ///
    /// Teardown order (matters for thread-safety):
    /// 1. Set `stopRequested` so the realtime tap callback drops new buffers.
    /// 2. `removeTap` — no further tap callbacks will be scheduled.
    /// 3. `engine.stop()` — flushes the audio graph.
    /// 4. `ioQueue.sync { }` — barrier; waits for any tap-dispatched writes
    ///    that already enqueued before stopRequested was set.
    /// 5. Release `audioFile` — flushes Int16 conversion + closes the WAV.
    /// 6. Read `frameCounter` and `writeError` — single-threaded read after
    ///    queue drain, no further mutation possible.
    ///
    /// - Returns: Int16 PCM WAV `Data` at native sample rate / channel count.
    /// - Throws:
    ///   - `DictationError.notRecording` if `start()` was never called.
    ///   - `DictationError.writeFailed(_)` if any tap-side `AVAudioFile.write`
    ///     failed (disk full, ENOSPC, EBUSY, etc.) — these are no longer
    ///     swallowed.
    ///   - `DictationError.emptyRecording` if too few frames were captured
    ///     (e.g. user released FN within 50ms of pressing it, or mic permission
    ///     was revoked mid-recording).
    func stop() async throws -> Data {
        guard isRecording else {
            throw DictationError.notRecording
        }

        // Set isRecording=false as the LAST mutation so a re-entrant start()
        // can't observe partial cleanup. Also clear the device-override swallow
        // flag — if it's still armed at stop() time the synthetic configChange
        // never fired; leaving it true would leak into the next session.
        defer {
            pendingDeviceOverride = false
            isRecording = false
        }

        // (1) Fence: tap callback checks this and drops in-flight buffers.
        stopRequested.withLock { $0 = true }

        // (2) Remove tap before stopping the engine. Already-fired tap callbacks
        // may still be running on the audio render thread; the dispatch onto
        // ioQueue.async means their writes are queued, not synchronous.
        engine.inputNode.removeTap(onBus: 0)

        // (3) Stop the engine. Does not synchronously wait for tap callbacks.
        engine.stop()

        // (4) Drain ioQueue: blocks until every previously-enqueued tap write
        // has completed. The stopRequested fence in the tap means no NEW
        // writes will be enqueued. After this barrier returns, audioFile is
        // safe to release.
        ioQueue.sync { /* barrier — drain any in-flight writes */ }

        // Tear down the configuration-change observer.
        if let token = configChangeObserver {
            NotificationCenter.default.removeObserver(token)
            configChangeObserver = nil
        }

        // (5) Releasing the AVAudioFile flushes Int16 conversion and closes
        // the WAV header. After ioQueue drain this is race-free.
        audioFile = nil

        // (6) Read final state. No further mutation; queue is drained.
        let frames = frameCounter.withLock { $0 }
        let writeFailure = writeError.withLock { $0 }
        let preClampPeak = floatPeak.withLock { $0 }
        let url = audioFileURL
        audioFileURL = nil
        let sampleRate = inputSampleRate
        let channels = inputChannelCount

        let clampNote = preClampPeak > 0.95 ? " (CLAMP ENGAGED — mic delivered out-of-range floats)" : ""
        Log.info(
            "DictationRecorder: stopped, \(frames) frames at \(Int(sampleRate))Hz \(channels)ch. " +
            "peak=\(String(format: "%.3f", preClampPeak))\(clampNote)",
            category: "capture"
        )

        // Surface any tap-side write error first — it explains why the WAV
        // may be short or zero-length.
        if let writeFailure {
            if let url { try? FileManager.default.removeItem(at: url) }
            throw DictationError.writeFailed(writeFailure)
        }

        // Empty-recording detection. Threshold ≈ 50ms at the native rate.
        // If the user tapped FN without speaking, or the mic was unavailable,
        // we'd otherwise ship a 44-byte header-only WAV that the server
        // returns empty text for and the user gets no feedback.
        let minFrames = AVAudioFrameCount(max(sampleRate * 0.05, 1))
        if frames < minFrames {
            if let url { try? FileManager.default.removeItem(at: url) }
            throw DictationError.emptyRecording
        }

        guard let url else {
            // Internal-state inconsistency — should be impossible after the
            // happy path, but reuse notRecording rather than fabricating a
            // start-path error.
            throw DictationError.notRecording
        }

        return try await Task.detached(priority: .userInitiated) {
            defer { try? FileManager.default.removeItem(at: url) }
            let data = try Data(contentsOf: url)

            // Post-write byte-level diagnostic intentionally omitted: Float32
            // WAVs include extended fmt/fact chunks of variable size, so a
            // naive `data.advanced(by: 44)` read produces garbage from chunk
            // headers, not actual samples. The pre-clamp float peak captured
            // in the tap callback (logged in the "stopped" line above) is the
            // accurate per-recording level reading.
            Log.debug(
                "DictationRecorder: WAV bytes=\(data.count)",
                category: "capture"
            )

            // DEBUG-only diagnostic copy. Production builds NEVER write user
            // audio outside the upload path. Originally this leaked every
            // dictation to /tmp/wispralt-last-dictation.wav unconditionally.
            #if DEBUG
            try? data.write(to: URL(fileURLWithPath: "/tmp/wispralt-last-dictation.wav"))
            #endif

            return data
        }.value
    }

    deinit {
        // Belt-and-suspenders: if the recorder is deallocated while still
        // running (controller swap, app teardown), drop the engine and clear
        // the observer so we don't leak threads or notification subscriptions.
        if isRecording {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        if let token = configChangeObserver {
            NotificationCenter.default.removeObserver(token)
        }
        if let url = audioFileURL {
            try? FileManager.default.removeItem(at: url)
        }
    }

}
