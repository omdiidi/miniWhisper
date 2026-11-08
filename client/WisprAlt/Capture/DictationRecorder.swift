import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation
import os.lock
import WisprAltCore

extension Notification.Name {
    /// Posted when AVAudioEngine's input configuration changes mid-recording
    /// (e.g. user plugs in / unplugs AirPods, switches default input device).
    /// MenuBarController observes this to abort the in-flight dictation and
    /// return the UI to idle — the engine has already invalidated the tap by
    /// the time this fires.
    static let dictationConfigChanged = Notification.Name("co.wispralt.dictationConfigChanged")
    /// Posted once when a dictation reaches the `maxSeconds` given to `start`.
    /// `userInfo["seconds"]` carries that cap (Int). The recorder stops
    /// buffering audio at that point; the UI layer treats it like an FN release.
    static let dictationMaxDurationReached = Notification.Name("co.wispralt.dictationMaxDurationReached")
}

// MARK: - DictationRecorder

/// Records microphone audio via `AVAudioEngine` into a native-rate Float32
/// WAV and hands it over from `stop()` as a `RecorderOutput`: the recording on
/// disk (the Online provider converts it to 16 kHz mono with `Mono16kWAV`)
/// and, for a Local capture only, a `LocalCapture` holding the streaming
/// session plus **16-bit PCM** WAVs at the native rate / channel count (the
/// server resamples to 16 kHz and averages channels).
///
/// The Float→Int16 downcast for Local happens AFTER the file is written, in
/// user code (`Int16WAVEncoder`), entirely outside AVAudioFile's writer path.
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
/// ## Lifetime
/// Typical usage: create once; call `start()` on FN key-down, `stop()` on
/// FN key-up. `stop()` is `async` so the file read off disk doesn't block
/// the caller's actor.
final class DictationRecorder {

    // MARK: - Errors

    enum DictationError: Error, LocalizedError {
        case engineStartFailed(Error)
        case notRecording
        case writeFailed(Error)
        case emptyRecording
        /// The Float32 → Int16 re-encode for a Local upload failed.
        case encodeFailed

        var errorDescription: String? {
            switch self {
            case .engineStartFailed(let err):
                return "AVAudioEngine failed to start: \(err.localizedDescription)"
            case .notRecording:
                return "DictationRecorder is not currently recording."
            case .writeFailed(let err):
                return "Audio file write failed: \(err.localizedDescription)"
            case .emptyRecording:
                return "Recording was empty — no audio captured."
            case .encodeFailed:
                return "Could not encode the recording for upload."
            }
        }

        /// The config-change observer records a mid-recording device change as
        /// `engineStartFailed(NSError code -11)`.
        static func isDeviceChange(_ error: Error?) -> Bool {
            guard case .engineStartFailed(let inner)? = error as? DictationError else { return false }
            return (inner as NSError).code == -11
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

    /// "A capture is running" plus the Local streaming session it owns.
    /// `begin` is the last step of `start()`; `stop()` copies every
    /// per-dictation value before `end()`, after which `start()` may run.
    private let slot = CaptureSlot<DictationStreamSession>()

    /// True when this capture was started with a Local endpoint (snapshot at
    /// `start()`); only then does `stop()` build a `LocalCapture`.
    private var isLocalCapture = false

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

    /// Guards `chunkBuffer` and the other per-session streaming state
    /// (vadState, counters). Held for the whole streaming section of the tap
    /// callback; stop() takes it as a fence after removing the tap so a
    /// callback that already passed `stopRequested` finishes before the tail
    /// is read or the buffer is cleared. The tap re-checks `stopRequested`
    /// after acquiring it, so no callback mutates the buffer after the fence.
    private let chunkLock = OSAllocatedUnfairLock()

    /// Monotonically increasing per-session chunk index passed to
    /// `DictationStreamSession.enqueueChunk(_:index:)`.
    private var nextLocalIndex: Int = 0

    /// Sum of all chunk audio durations sent so far this session, in
    /// milliseconds. Used to short-circuit additional cuts once we approach
    /// the 270 s server-side cumulative cap.
    private var cumulativeChunkAudioMs: Double = 0

    /// Off the realtime render thread, sequenced via this serial queue.
    /// Encodes Float32 frames → Int16 WAV and registers the chunk with the
    /// streaming session via a tiny Task wrapper + DispatchGroup.wait()
    /// (registration synchronization — see DictationStreamSession.enqueueChunk
    /// for the actor-isolation contract).
    private let chunkEncodeQueue = DispatchQueue(
        label: "co.wispralt.dictation.chunkEncode",
        qos: .userInitiated
    )

    private let maxDurationNotified = OSAllocatedUnfairLock<Bool>(initialState: false)

    /// Flips true once the first chunk has been registered with the actor:
    /// "streaming has actually engaged". Sub-8 s dictations never set it and
    /// take the whole-file upload (`LocalFinalizeRoute`).
    private let streamingArmedAtomic = OSAllocatedUnfairLock<Bool>(initialState: false)

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

    /// Trailing-audio frames not yet shipped as a chunk (the `/finalize`
    /// tail). CONSUMING: hands the samples over and releases the recorder's
    /// copy (`= []`, not keepingCapacity) so a long dictation's audio does not
    /// stay resident while idle. Called once by `stop()` before `slot.end()`.
    private func takeChunkBuffer() -> [Float] {
        chunkLock.lock()
        defer { chunkLock.unlock() }
        let tail = chunkBuffer
        chunkBuffer = []
        return tail
    }

    // MARK: - Public API

    /// Starts microphone capture.
    ///
    /// - Parameters:
    ///   - dictationId: the dictation's id (names the temp recording).
    ///   - endpoint: the Local server, or nil. Local streaming is armed only
    ///     when this is non-nil, so an Online dictation never streams.
    ///   - maxSeconds: the dictation cap; audio past it is dropped and
    ///     `.dictationMaxDurationReached` is posted once.
    /// - Returns: `true` when capture started; `false` while the previous
    ///   capture has not finished handing off (`stop()` before `slot.end()`).
    /// - Throws: `DictationError.engineStartFailed` if the audio engine cannot
    ///   start. Every throw happens before `slot.begin`, so the slot stays free.
    func start(dictationId: String, endpoint: ServerEndpoint?, maxSeconds: Int) throws -> Bool {
        guard !slot.isActive else { return false }

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
        // a real device change (safe default: end the recording), which is correct behavior
        // for any change that fires before the engine is even running.

        let inputNode = engine.inputNode

        // === Apply preferred input device BEFORE format read & observer install.
        // The format read MUST happen on the new device — switching device changes
        // sample rate / channel count. The configChange observer installed below
        // would also catch our override and end the recording, so we do this
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
                // would catch it and end the recording. Set the swallow flag
                // BEFORE the observer is installed, AND record when so the
                // observer only swallows within `overrideSwallowWindow`. If the
                // synthetic callback is lost or never fires, the flag self-times-
                // out and a later real device change correctly ends the session.
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
        maxDurationNotified.withLock { $0 = false }
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
        // Local streaming is armed only for a Local endpoint. The session's
        // init does no I/O, so a throw below simply drops it. A previous
        // dictation's session is never touched here: `stop()` handed it off.
        let session = endpoint.map {
            DictationStreamSession(endpoint: $0, dictationId: dictationId, speechStartedAt: Date())
        }
        isLocalCapture = endpoint != nil

        // Write directly in the input format. The server (soundfile + librosa)
        // resamples to 16 kHz internally, so we don't need to do it client-side.
        // Avoiding AVAudioConverter eliminates a class of conversion bugs that
        // produced out-of-range float values (peak >> 1.0).
        //
        // Channel count: we keep the native channel count rather than forcing
        // mono. Mono downmix happens server-side via `np.mean(axis=1)` in
        // `wispralt/audio.py`, which is the mathematically correct
        // average. AVAudioConverter's default channel-mix on macOS sums
        // channels without averaging — that bug is exactly what produced the
        // peak=3.974 floats we just removed.
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("wispralt_dictation_\(dictationId).wav")
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
            Log.error("DictationRecorder: failed to open AVAudioFile: \(ErrorLog.describe(error))", category: "capture")
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
            // device change correctly ends it (better to end one recording
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
            // even after stop() — without this guard they'd end the new
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
            // Notify the UI layer so it can stop and submit the
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
        let capFrames = UInt32(min(inputFormat.sampleRate * Double(maxSeconds), Double(UInt32.max)))
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            // Stop fence: drop any buffers that race in after stop() began.
            if self.stopRequested.withLock({ $0 }) { return }

            // Hard duration cap (missed FN-up / runaway recording). Drop all
            // further audio so file, chunk buffer and the later WAV encode
            // stay bounded; notify once so the UI can stop + submit.
            if self.frameCounter.withLock({ $0 }) >= capFrames {
                if self.maxDurationNotified.withLock({ let was = $0; $0 = true; return !was }) {
                    Log.warning("DictationRecorder: max dictation duration reached; dropping further audio.", category: "capture")
                    DispatchQueue.main.async {
                        NotificationCenter.default.post(name: .dictationMaxDurationReached, object: nil,
                                                        userInfo: ["seconds": maxSeconds])
                    }
                }
                return
            }

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
                    Log.error("DictationRecorder: write failed: \(ErrorLog.describe(error))", category: "capture")
                    self.writeError.withLock { existing in
                        if existing == nil { existing = error }
                    }
                }
            }

            // === Streaming dictation (Local only). ==========================
            // Only emit chunks when this capture has a Local session.
            // The classify/append/cut sequence is intentionally inline on the
            // tap thread — none of it blocks (no I/O, no locks held across
            // boundaries) so realtime safety is preserved. WAV encoding and
            // network registration happen on chunkEncodeQueue (off-thread).
            if let session {
                self.chunkLock.lock()
                defer { self.chunkLock.unlock() }
                if self.stopRequested.withLock({ $0 }) { return }
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
                    let cutFrames = Array(self.chunkBuffer[..<cutSampleCount])
                    if EnergyVAD.averageRmsDB(cutFrames) < EnergyVAD.minChunkAverageDB {
                        break
                    }
                    self.chunkBuffer.removeSubrange(0..<cutSampleCount)
                    self.emitChunk(cutFrames, session: session)
                }

                // Memory cap: never let unsent audio grow unbounded. Ship the
                // pending audio as a chunk (dropping it only if it is pure
                // silence) rather than discarding speech.
                if self.chunkBuffer.count > Self.maxPendingSamples(
                    sampleRate: self.inputSampleRate,
                    channels: Int(self.inputChannelCount)
                ) {
                    self.flushOverCap(session: session)
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

        _ = slot.begin(session)
        Log.info(
            "DictationRecorder: recording started (input: \(Int(inputFormat.sampleRate))Hz \(inputFormat.channelCount)ch).",
            category: "capture"
        )
        return true
    }

    /// Stops capture and hands the recording over.
    ///
    /// Teardown order (the contract):
    /// 1. Set `stopRequested` so the realtime tap callback drops new buffers.
    /// 2. `removeTap`, then `engine.stop()`.
    /// 3. Take `chunkLock` once (fences a tap callback already past the check).
    /// 4. Drain `ioQueue`, then `chunkEncodeQueue` (late chunks registered).
    /// 5. Release `audioFile` (closes the WAV).
    /// 6. Copy EVERY per-dictation value into locals, then `slot.end()`.
    ///    From here on `start()` may run for the next dictation, so only the
    ///    locals are used below.
    ///
    /// - Returns: the native-rate Float32 recording on disk (the caller deletes
    ///   it) and, for a Local capture, the `LocalCapture` (Int16 WAVs, session).
    /// - Throws: `notRecording`; `writeFailed(_)` for a tap write error or a
    ///   device change; `emptyRecording` under ~50 ms; `encodeFailed` when the
    ///   Local re-encode fails. On any throw the taken session is aborted and
    ///   the temp file deleted.
    func stop() async throws -> RecorderOutput {
        guard slot.isActive else { throw DictationError.notRecording }

        stopRequested.withLock { $0 = true }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        chunkLock.lock()
        chunkLock.unlock()
        ioQueue.sync { /* drain in-flight file writes */ }
        chunkEncodeQueue.sync { /* drain chunk encodes + actor registrations */ }
        if let token = configChangeObserver {
            NotificationCenter.default.removeObserver(token)
            configChangeObserver = nil
        }
        audioFile = nil

        // ── every per-dictation value is copied BEFORE end() ──
        let url = audioFileURL
        audioFileURL = nil
        let frames = frameCounter.withLock { $0 }
        let failure = writeError.withLock { $0 }
        let preClampPeak = floatPeak.withLock { $0 }
        let rate = inputSampleRate
        let channels = inputChannelCount
        let isLocal = isLocalCapture
        let armed = streamingArmedAtomic.withLock { $0 }
        let tail = takeChunkBuffer()            // also releases the buffer when not armed
        pendingDeviceOverride = false
        pendingDeviceOverrideSetAt = 0
        let session = slot.end()                // start() may run from here on; locals only below

        let clampNote = preClampPeak > 0.95 ? " (CLAMP ENGAGED — mic delivered out-of-range floats)" : ""
        Log.info(
            "DictationRecorder: stopped, \(frames) frames at \(Int(rate))Hz \(channels)ch. " +
            "peak=\(String(format: "%.3f", preClampPeak))\(clampNote)",
            category: "capture"
        )

        let interrupted = DictationError.isDeviceChange(failure)
        do {
            if let failure { throw DictationError.writeFailed(failure) }
            // ≈ 50 ms at the native rate: an FN tap without speech, or no mic.
            guard let url, frames >= AVAudioFrameCount(max(rate * 0.05, 1)) else {
                throw DictationError.emptyRecording
            }
            var local: LocalCapture? = nil
            if isLocal {
                local = try await LocalCapture.make(url: url, rate: rate, channels: Int(channels),
                                                    armed: armed, tail: tail, session: session)
            }
            return RecorderOutput(
                recording: Recording(fileURL: url, durationSeconds: Double(frames) / rate,
                                     interruptedByDeviceChange: interrupted),
                local: local
            )
        } catch {
            abortTakenSession(session)
            if let url { try? FileManager.default.removeItem(at: url) }
            throw error
        }
    }

    /// Aborts a session `stop()` took from the slot but will not hand on.
    private func abortTakenSession(_ session: DictationStreamSession?) {
        guard let session else { return }
        Task { await session.abort() }
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
        // No exact-size reserveCapacity: that defeats Array's geometric growth
        // and reallocates + copies the whole buffer on every ~85 ms callback.
        for f in 0..<frameCount {
            for ch in 0..<channels {
                chunkBuffer.append(channelData[ch][f])
            }
        }
    }

    /// ~2 minutes of audio at the native rate/channels, in interleaved samples.
    private static func maxPendingSamples(sampleRate: Double, channels: Int) -> Int {
        Int(sampleRate * 120) * max(channels, 1)
    }

    /// Records the chunk in the session counters and hands it to the encode
    /// queue. Must be called with `chunkLock` held (tap thread).
    private func emitChunk(_ cutFrames: [Float], session: DictationStreamSession) {
        let channels = Int(self.inputChannelCount)
        let sampleRate = self.inputSampleRate
        self.cumulativeChunkAudioMs += Double(cutFrames.count / max(channels, 1))
            / sampleRate * 1000.0
        self.speechSinceLastCutMs = 0
        self.chunkEncodeQueue.async { [weak self] in
            guard let self else { return }
            let idx = self.nextLocalIndex
            self.nextLocalIndex += 1
            let wav = Int16WAVEncoder.encodeFloat32Frames(
                cutFrames,
                sampleRate: sampleRate,
                channels: channels
            )
            // SYNCHRONOUS actor registration: the DispatchGroup waits until
            // the actor's enqueueChunk(_:index:) returns (i.e. inFlight[idx]
            // has been set), so a subsequent finalize() call sees this chunk
            // no matter how fast the cuts come.
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

    /// Called when `chunkBuffer` exceeds the pending cap. Ships everything as a
    /// chunk if any 0.5 s window carries audio above the silence gate; if the
    /// whole buffer is silence it is dropped. If the cumulative chunk cap is
    /// already reached the audio stays for the finalize tail (bounded by the
    /// 600 s dictation cap). Must be called with `chunkLock` held.
    private func flushOverCap(session: DictationStreamSession) {
        if cumulativeChunkAudioMs > 870_000 { return }
        let channels = max(Int(inputChannelCount), 1)
        let window = Int(inputSampleRate * 0.5) * channels
        var hasAudio = false
        var i = 0
        while i < chunkBuffer.count {
            let end = min(i + window, chunkBuffer.count)
            if EnergyVAD.averageRmsDB(Array(chunkBuffer[i..<end])) >= EnergyVAD.minChunkAverageDB {
                hasAudio = true
                break
            }
            i = end
        }
        let frames = chunkBuffer
        chunkBuffer = []
        if hasAudio {
            emitChunk(frames, session: session)
        } else {
            Log.info("DictationRecorder: dropped >120 s of pure silence from chunk buffer.", category: "capture")
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
        if slot.isActive {
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

// MARK: - Recorder output (app only; never enters WisprAltCore)

/// What a Local dictation needs after FN-up: the streaming session (if one
/// was armed at `start`), whether a chunk went out, and the Int16 WAVs.
struct LocalCapture {
    let session: DictationStreamSession?
    /// At least one chunk was registered with `session`.
    let armed: Bool
    /// Native-rate Int16 WAV of the audio not yet sent as a chunk (finalize tail).
    let tailWAV: Data
    /// Native-rate Int16 WAV of the whole recording (whole-file upload / fallback).
    let wholeWAV: Data

    /// Re-encodes off the caller's actor. The server resamples and downmixes,
    /// so the Local upload stays native-rate (no client conversion).
    static func make(url: URL, rate: Double, channels: Int, armed: Bool, tail: [Float],
                     session: DictationStreamSession?) async throws -> LocalCapture {
        try await Task.detached(priority: .userInitiated) {
            let whole: Data
            do {
                whole = try Int16WAVEncoder.encode(fromFloat32WAVAt: url)
            } catch {
                Log.error("DictationRecorder: Int16 re-encode failed: \(ErrorLog.describe(error))", category: "capture")
                throw DictationRecorder.DictationError.encodeFailed
            }
            let tailWAV = armed
                ? Int16WAVEncoder.encodeFloat32Frames(tail, sampleRate: rate, channels: channels)
                : Data()
            return LocalCapture(session: session, armed: armed, tailWAV: tailWAV, wholeWAV: whole)
        }.value
    }
}

struct RecorderOutput {
    let recording: Recording
    /// Non-nil only for a capture started with a Local endpoint.
    let local: LocalCapture?
}

extension DictationRecorder.DictationError: LogSafeError {
    var logDescription: String {
        switch self {
        case .engineStartFailed(let e): return "engineStartFailed(\(ErrorLog.describe(e)))"
        case .notRecording: return "notRecording"
        case .writeFailed(let e): return "writeFailed(\(ErrorLog.describe(e)))"
        case .emptyRecording: return "emptyRecording"
        case .encodeFailed: return "encodeFailed"
        }
    }

    var outcomeKind: ErrorOutcome {
        switch self {
        case .emptyRecording: return .empty
        case .writeFailed(let e): return Self.isDeviceChange(e) ? .failed(.deviceChanged) : .failed(.writeFailed)
        case .encodeFailed: return .failed(.encodeFailed)
        case .engineStartFailed, .notRecording: return .failed(.other)
        }
    }
}
