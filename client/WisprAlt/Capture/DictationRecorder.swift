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
/// **16-bit PCM** WAV (typically 48 kHz, native channel count) suitable for
/// the `/transcribe/dictate` endpoint. Server (`audio.py`) handles the
/// resample to 16 kHz and the multi-channel → mono downmix via `np.mean`.
///
/// On-disk capture is Float32 PCM (see "Why Float32 + native rate" below);
/// the Float→Int16 downcast happens AFTER the file is written, in user code
/// (see `Int16WAVEncoder`), entirely outside AVAudioFile's writer path. This
/// halves the upload payload (~50% smaller) with zero perceptual quality
/// loss — Parakeet is trained on 16-bit audio and the server resamples to
/// 16 kHz, so any precision above the 16-bit noise floor is dropped anyway.
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

    /// Native input sample rate. Internal so the streaming-dictation path
    /// (`DictationAPI.transcribe`) can encode the tail WAV at the same rate
    /// as the in-flight chunks.
    private(set) var inputSampleRate: Double = 0

    /// Native input channel count. Internal for the same reason as
    /// `inputSampleRate`. We do NOT downmix client-side — the server averages
    /// channels via np.mean.
    private(set) var inputChannelCount: AVAudioChannelCount = 1

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
    /// first such notification per recording session — but ONLY within a
    /// short time window (`overrideSwallowWindow`) of when the override was
    /// applied. Beyond that window the synthetic callback is presumed to have
    /// been lost, and any subsequent configChange is treated as a real device
    /// change (the safer default — better to abort one recording than to
    /// silently capture audio from a now-disconnected device).
    private var pendingDeviceOverride: Bool = false
    private var pendingDeviceOverrideSetAt: TimeInterval = 0
    private static let overrideSwallowWindow: TimeInterval = 0.05

    /// Wall-clock time (`Date().timeIntervalSinceReferenceDate`) stamped
    /// IMMEDIATELY after `engine.start()` returns successfully. The settling
    /// window check (`configChangeSettleWindow`) is measured from this stamp.
    ///
    /// Why after engine.start() and not before: `engine.start()` blocks the
    /// main thread for 50–200 ms on a cold input device while the audio HAL
    /// negotiates sample rate / channel count. AVAudioEngine fires
    /// `AVAudioEngineConfigurationChange` DURING that block as part of cold-
    /// device renegotiation, but the observer block is queued to `.main` and
    /// can't execute until `engine.start()` returns. If we stamped the start
    /// time before the call, `elapsed` would already exceed the 100 ms window
    /// by the time the observer runs — so the cold-start configChange leaks
    /// through and aborts the session ("first hold doesn't record" symptom).
    ///
    /// Stamping after `engine.start()` returns means `elapsed ≈ 0` for any
    /// configChange that was queued during the cold-start phase, and the
    /// settling window correctly swallows it.
    ///
    /// The window's purpose: drop AVAudioEngineConfigurationChange notifications
    /// that arrive in the first 100 ms of a session. Two sources:
    ///   1. Cold-device renegotiation during engine.start() (the dominant case).
    ///   2. Stale cross-session callbacks from a previous session's device
    ///      override that landed after stop() returned.
    ///
    /// Trade-off: under heavy main-thread pressure, an in-session synthetic
    /// callback can also arrive past the window — in which case the next-arriving
    /// real device change is treated as the swallow target instead. The
    /// `pendingDeviceOverride` time-bound (50 ms from override-set time) limits
    /// this failure mode further: even if the configChangeSettleWindow misses,
    /// `pendingDeviceOverride` self-times-out and any real change after 50 ms
    /// from the override correctly aborts.
    ///
    /// Alternative considered and rejected: per-session AVAudioEngine
    /// reinstantiation (the only way to fully distinguish stale-cross-session
    /// callbacks from in-session ones). Rejected because audio-graph rebuild on
    /// every FN press is a much higher cost than the rare false-positive abort.
    private var sessionStartTime: TimeInterval = 0
    private static let configChangeSettleWindow: TimeInterval = 0.1

    // MARK: - Streaming dictation state (Phase 2, opt-in)

    /// VAD state (rolling noise floor, hysteresis flags). Reset at start().
    private var vadState = EnergyVAD.State()

    /// Total session wall-time in audio milliseconds. Reset at start().
    private var sessionElapsedMs: Double = 0

    /// Audio milliseconds spent in speech since the last emitted cut. Reset
    /// at start() and after each cut. Drives the chunkMinSpeechMs /
    /// chunkHardCapMs gates in EnergyVAD.classify(...).
    private var speechSinceLastCutMs: Double = 0

    /// Interleaved Float32 samples accumulated since the last cut. Drained
    /// at cut time into the chunk encoder; remaining samples form the
    /// finalize-tail payload.
    private var chunkBuffer: [Float] = []

    /// Monotonically increasing per-session chunk index passed to
    /// `DictationStreamSession.enqueueChunk(_:index:)`.
    private var nextLocalIndex: Int = 0

    /// Sum of all chunk audio durations sent so far this session, in
    /// milliseconds. Used to short-circuit additional cuts once we approach
    /// the 270 s server-side cumulative cap.
    private var cumulativeChunkAudioMs: Double = 0

    /// The streaming session for the in-progress recording, or nil when
    /// streaming is disabled for this session. Surfaced read-only so
    /// `DictationAPI.transcribe(_:recorder:)` can call `finalize(...)` and
    /// later `abort()`.
    private(set) var streamSession: DictationStreamSession?

    /// Off the realtime render thread, sequenced via this serial queue.
    /// Encodes Float32 frames → Int16 WAV and registers the chunk with the
    /// streaming session via a tiny Task wrapper + DispatchGroup.wait()
    /// (registration synchronization — see DictationStreamSession.enqueueChunk
    /// for the actor-isolation contract).
    private let chunkEncodeQueue = DispatchQueue(
        label: "co.wispralt.dictation.chunkEncode",
        qos: .userInitiated
    )

    /// Flips true once the first chunk has been registered with the actor.
    /// Read by `DictationAPI.transcribe(_:recorder:)` as the "streaming has
    /// actually engaged" gate — sub-8 s dictations never set this and so
    /// silently take the existing non-streaming ladder.
    private let streamingArmedAtomic = OSAllocatedUnfairLock<Bool>(initialState: false)

    /// SNAPSHOT of `Settings.shared.streamingDictation` at `start()` time.
    /// Read by `DictationAPI.transcribe(_:recorder:)` so a mid-recording
    /// toggle by the user cannot orphan an already-open server session.
    private(set) var streamingEnabledForThisSession: Bool = false

    /// Public accessor for the streamingArmed flag. Reads under the lock.
    var streamingArmed: Bool { streamingArmedAtomic.withLock { $0 } }

    /// One latency reading captured by MenuBarController after paste
    /// completes. Phase 4.4 reads `~/Library/Caches/WisprAlt/latency.json`
    /// for A/B timing without flush-nondeterminism from `Log.info`.
    struct LatencyReading: Codable, Sendable {
        let fnRelease: Date
        let paste: Date
        let dedupId: String?
    }

    /// Capped-100-entry FIFO of latency readings. `appendLatencyReading`
    /// trims as it appends; persistence happens on every append, debounced
    /// to 1 Hz via `latencyPersistDebounce`.
    private let latencyRing = OSAllocatedUnfairLock<[LatencyReading]>(initialState: [])
    private let latencyPersistDebounce = OSAllocatedUnfairLock<Date?>(initialState: nil)

    /// Append a latency reading and (debounced 1 Hz) persist the ring to
    /// `~/Library/Caches/WisprAlt/latency.json`. Safe to call from any
    /// thread.
    func appendLatencyReading(_ r: LatencyReading) {
        latencyRing.withLock { ring in
            ring.append(r)
            if ring.count > 100 { ring.removeFirst() }
        }
        persistLatencyRingIfDue()
    }

    /// Snapshot copy of the latency ring. Used by Phase 4.4 telemetry.
    func snapshotLatencyReadings() -> [LatencyReading] {
        latencyRing.withLock { $0 }
    }

    private func persistLatencyRingIfDue() {
        let now = Date()
        let due: Bool = latencyPersistDebounce.withLock { last in
            if let last, now.timeIntervalSince(last) < 1.0 { return false }
            last = now
            return true
        }
        guard due else { return }
        let cachesDir = FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask).first!
            .appendingPathComponent("WisprAlt", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: cachesDir,
            withIntermediateDirectories: true
        )
        let target = cachesDir.appendingPathComponent("latency.json")
        let tmp = target.appendingPathExtension("tmp")
        let readings = latencyRing.withLock { $0 }
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(readings) else { return }
        try? data.write(to: tmp, options: .atomic)
        try? FileManager.default.removeItem(at: target)
        try? FileManager.default.moveItem(at: tmp, to: target)
    }

    /// Trailing-audio frames not yet shipped as a chunk. Encoded into the
    /// `/finalize` request's `file` part by `DictationAPI.transcribe`.
    func tailFrames() -> [Float] { chunkBuffer }

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

        // sessionStartTime is stamped AFTER engine.start() returns successfully
        // (see the property's doc-comment for why). Until then, leave whatever
        // value was there — if a configChange somehow fires before we stamp the
        // new value, `elapsed = now - stale` is huge and the event is treated as
        // a real device change (safe-abort default), which is correct behavior
        // for any change that fires before the engine is even running.

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
                // BEFORE the observer is installed, AND record when so the
                // observer only swallows within `overrideSwallowWindow`. If the
                // synthetic callback is lost or never fires, the flag self-times-
                // out and a later real device change correctly aborts the session.
                pendingDeviceOverride = true
                pendingDeviceOverrideSetAt = Date().timeIntervalSinceReferenceDate
                Log.info(
                    "DictationRecorder: input device set to UID \(preferredUID); next configChange within \(Int(Self.overrideSwallowWindow * 1000))ms will be swallowed.",
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

        // === Streaming dictation reset (Phase 2). ===
        // Reset all per-session streaming state BEFORE the tap fires so
        // there's no chance a leftover chunkBuffer or vadState from a prior
        // session pollutes the new one.
        vadState = EnergyVAD.State()
        sessionElapsedMs = 0
        speechSinceLastCutMs = 0
        chunkBuffer = []
        nextLocalIndex = 0
        cumulativeChunkAudioMs = 0
        streamingArmedAtomic.withLock { $0 = false }
        // v0.4.6: streaming is ALWAYS on. The user-facing toggle was removed.
        // Settings.streamingDictation property kept for back-compat but ignored
        // here. Streaming-then-fallback always wins over a single-POST given
        // the safety-buffer + dedup-id design, so there's no reason to gate it.
        streamingEnabledForThisSession = true
        if streamingEnabledForThisSession {
            // Defensive: a leftover session from a rapid double-tap-FN race
            // gets aborted here. The Task is fire-and-forget on purpose —
            // start() is sync (callers expect it), and the rare residual
            // race (old session POST lands on server before its cancellation
            // completes) is bounded by the server's per-user single-session
            // cap which silently falls back on the next recording.
            if let old = streamSession {
                Task { await old.abort() }
            }
            streamSession = DictationStreamSession(speechStartedAt: Date())
        } else {
            streamSession = nil
        }

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
            // real device-change event. BUT only within `overrideSwallowWindow`
            // of when we set pendingDeviceOverride; if the synthetic callback
            // never arrives in time, the flag self-times-out and a later real
            // device change correctly aborts (better to abort one recording
            // than to silently capture from a disconnected device).
            if self.pendingDeviceOverride {
                let sinceOverride = Date().timeIntervalSinceReferenceDate - self.pendingDeviceOverrideSetAt
                if sinceOverride <= Self.overrideSwallowWindow {
                    self.pendingDeviceOverride = false
                    Log.info(
                        "DictationRecorder: swallowed configChange from programmatic device override (\(Int(sinceOverride * 1000))ms).",
                        category: "capture"
                    )
                    return
                }
                // Fell out of the window — the synthetic callback was lost.
                // Clear the flag and treat THIS event as a real device change.
                self.pendingDeviceOverride = false
                Log.warning(
                    "DictationRecorder: pendingDeviceOverride timed out (\(Int(sinceOverride * 1000))ms); treating configChange as real.",
                    category: "capture"
                )
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

            // === Streaming dictation (opt-in, Phase 2). =====================
            // Only emit chunks when streaming was enabled at session start.
            // The classify/append/cut sequence is intentionally inline on the
            // tap thread — none of it blocks (no I/O, no locks held across
            // boundaries) so realtime safety is preserved. WAV encoding and
            // network registration happen on chunkEncodeQueue (off-thread).
            if let session = self.streamSession, self.streamingEnabledForThisSession {
                let vad = EnergyVAD()
                let event = vad.classify(
                    buffer: buffer,
                    state: &self.vadState,
                    sessionElapsedMs: self.sessionElapsedMs,
                    speechSinceLastCutMs: self.speechSinceLastCutMs
                )
                let bufferMs = Double(buffer.frameLength) / buffer.format.sampleRate * 1000.0
                self.sessionElapsedMs += bufferMs
                if self.vadState.inSpeech { self.speechSinceLastCutMs += bufferMs }

                // Always append interleaved Float32 frames to the per-session
                // buffer; the cut path slices off the leading prefix that
                // corresponds to the cut offset.
                self.appendFramesToChunkBuffer(buffer)

                switch event {
                case .noOp, .speechStart:
                    break
                case .cut(let offMs, _):
                    // v0.4.3: raised 270_000 → 870_000 (14.5 min). Mirrors the server-side
                    // dictation_max_duration_s - 30 s tail headroom (900 s cap). Supports
                    // 10+ minute recordings without the client refusing to emit chunks
                    // past 4.5 min.
                    if self.cumulativeChunkAudioMs > 870_000 {
                        // Stop emitting chunks; remaining audio rides the finalize tail.
                        break
                    }
                    let cutSampleCount = Self.framesUpTo(
                        offsetMs: offMs,
                        in: self.chunkBuffer,
                        sampleRate: self.inputSampleRate,
                        channels: Int(self.inputChannelCount)
                    )
                    // Empty/silence guard — never send Parakeet a silent
                    // buffer (it hallucinates short repeated words).
                    let minInterleavedSamples =
                        EnergyVAD.minChunkFrames * Int(self.inputChannelCount)
                    if cutSampleCount < minInterleavedSamples { break }
                    let cutFrames = Array(self.chunkBuffer.prefix(cutSampleCount))
                    if EnergyVAD.averageRmsDB(cutFrames) < EnergyVAD.minChunkAverageDB {
                        break
                    }
                    self.chunkBuffer.removeFirst(cutSampleCount)
                    let chunkAudioMs = Double(cutSampleCount / Int(self.inputChannelCount))
                        / self.inputSampleRate * 1000.0
                    self.cumulativeChunkAudioMs += chunkAudioMs
                    self.speechSinceLastCutMs = 0

                    // Snapshot the values needed off-thread; the recorder's
                    // sample rate / channel count won't change mid-session
                    // (configChange invalidates the tap and aborts), but
                    // explicit capture is cheaper to reason about.
                    let sampleRate = self.inputSampleRate
                    let channels = Int(self.inputChannelCount)
                    self.chunkEncodeQueue.async { [weak self] in
                        guard let self else { return }
                        let idx = self.nextLocalIndex
                        self.nextLocalIndex += 1
                        let wav = Int16WAVEncoder.encodeFloat32Frames(
                            cutFrames,
                            sampleRate: sampleRate,
                            channels: channels
                        )
                        // SYNCHRONOUS actor registration: the DispatchGroup
                        // waits until the actor's enqueueChunk(_:index:)
                        // returns (i.e. inFlight[idx] = t has been set), so
                        // a subsequent finalize() call sees this chunk in
                        // the in-flight dict no matter how fast the cuts come.
                        let group = DispatchGroup()
                        group.enter()
                        Task {
                            await session.enqueueChunk(wav, index: idx)
                            group.leave()
                        }
                        group.wait()
                        if idx == 0 {
                            self.streamingArmedAtomic.withLock { $0 = true }
                        }
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

        // Stamp NOW — same main-thread turn as the engine.start() return. Any
        // AVAudioEngineConfigurationChange queued onto .main during the cold-
        // start renegotiation will run only after this main turn completes, so
        // its `elapsed = now - sessionStartTime` reads ~0 ms and the settling
        // window correctly swallows it. See the property doc-comment for the
        // full reasoning.
        sessionStartTime = Date().timeIntervalSinceReferenceDate

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
    /// native channel count, **16-bit signed PCM** (interleaved). On disk the
    /// tap writes Float32 (the only known-safe AVAudioFile writer path); the
    /// returned bytes are Int16, downcast in `Int16WAVEncoder.encode` after
    /// the file is closed. See class doc-comment for the full rationale.
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
        // flag and its timestamp — if either is still armed at stop() time the
        // synthetic configChange never fired; leaving them set would leak into
        // the next session.
        defer {
            pendingDeviceOverride = false
            pendingDeviceOverrideSetAt = 0
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

        // (4b) Drain chunkEncodeQueue: blocks until every pending Float32 →
        // Int16 WAV encode + actor registration has completed. Without this,
        // DictationAPI.transcribe(_:recorder:) could call session.finalize()
        // before a late chunk has been registered with the actor, dropping
        // it from the in-flight wait-set.
        chunkEncodeQueue.sync { /* drain pending chunk encodes + actor registrations */ }

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

            // Read the Float32 WAV off disk, then downcast to a 16-bit PCM
            // WAV in-memory at the same sample rate and channel count. The
            // downcast halves the upload payload with zero perceptual quality
            // loss for speech — Parakeet is trained on 16-bit audio and the
            // server resamples to 16 kHz anyway, so any signal we'd preserve
            // in Float32 above the 16-bit noise floor is dropped server-side.
            //
            // The conversion happens entirely in user code (clamp + scale by
            // 32767 + cast to Int16 + hand-built WAV header). We do NOT use
            // AVAudioFile's writer to perform the Float→Int16 step — see the
            // class doc-comment on DictationRecorder for the long history of
            // failures with that path.
            let data: Data
            do {
                data = try Int16WAVEncoder.encode(fromFloat32WAVAt: url)
            } catch {
                Log.error(
                    "DictationRecorder: Int16 re-encode failed (\(error)); falling back to Float32 WAV bytes.",
                    category: "capture"
                )
                // If the re-encode fails for any reason, ship the original
                // Float32 WAV rather than failing the dictation. Quality is
                // the priority — server accepts both Float32 and Int16 WAVs.
                data = try Data(contentsOf: url)
            }

            Log.debug(
                "DictationRecorder: WAV bytes=\(data.count) (16-bit PCM)",
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

    // MARK: - Streaming helpers (Phase 2)

    /// Append the tap buffer's planar Float32 samples into `chunkBuffer`
    /// in interleaved layout (frame * channels + ch). The recorder writes
    /// `interleaved: false` to disk, but the streaming encoder expects
    /// interleaved samples — interleaving at append time keeps the cut
    /// path a simple `prefix(n)` slice.
    private func appendFramesToChunkBuffer(_ buffer: AVAudioPCMBuffer) {
        guard let channelData = buffer.floatChannelData else { return }
        let frameCount = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        chunkBuffer.reserveCapacity(chunkBuffer.count + frameCount * channels)
        for f in 0..<frameCount {
            for ch in 0..<channels {
                chunkBuffer.append(channelData[ch][f])
            }
        }
    }

    /// Convert a cut-offset expressed in **audio milliseconds** to the
    /// INTERLEAVED-sample count it corresponds to inside `buffer`. One
    /// audio frame = `channels` interleaved samples, so the math is:
    ///     frames = floor(offsetMs / 1000 * sampleRate)
    ///     interleavedSamples = frames * channels
    /// The result is clamped to `buffer.count` to defend against rounding
    /// pushing us off the end of the actual sample buffer.
    private static func framesUpTo(
        offsetMs: Double,
        in buffer: [Float],
        sampleRate: Double,
        channels: Int
    ) -> Int {
        let frames = Int(offsetMs / 1000.0 * sampleRate)
        return min(frames * channels, buffer.count)
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
