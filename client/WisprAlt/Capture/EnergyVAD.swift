import AVFoundation
import Foundation

/// Energy-based voice activity detector for streaming dictation.
///
/// Computes RMS over short windows, tracks a rolling noise floor, applies
/// asymmetric hysteresis (enter speech high, exit speech low) to suppress
/// chatter, and emits cut events on silence boundaries or a 30 s hard cap.
///
/// All thresholds are noise-floor-relative so the detector adapts to varying
/// microphone setups (AirPods vs MacBook built-in vs USB condenser).
/// Phase 0 calibration may revise the constants; checked-in values are
/// starting points.
struct EnergyVAD {
    static let speechEnterThresholdAboveNoiseFloorDB: Float = 14
    static let speechExitThresholdAboveNoiseFloorDB: Float = 8
    static let noiseFloorWindowMs: Double = 1_000
    static let noiseFloorFallbackDB: Float = -55
    // v0.4.4 tuning (2026-05-19): lowered 400→250 ms per user request. Catches even
    // shorter natural breaths (mid-sentence comma-pauses, quick inter-clause gaps).
    // No word-loss risk — the recorder writes every frame to the audio buffer
    // regardless of VAD state; the hangover only decides when to fire a CUT event.
    // 250 ms is well above typical inter-syllable gaps (50-150 ms), so we won't
    // chatter-cut inside words.
    // History: 600 (v0.4.0, original) → 400 (v0.4.1, after first real-user test) → 250 (v0.4.4).
    static let silenceHangoverMs: Double = 250
    static let minSpeechBeforeStreamMs: Double = 8_000   // bypass threshold (Plan constraint #4)
    // v0.4.2: lowered 20_000 → 5_000 per first-principles take. Smaller chunks
    // mean MORE chunks fire during recording (each transcribes in ~1.5 s on
    // M4 Parakeet, fully overlapped with continued speech), so the tail on
    // FN release is always small and finalize is ~1-2 s. Quality unchanged —
    // cuts still land on natural silences, never mid-word. Mercury (when on)
    // re-joins the seams on the concatenated text. Paired with the
    // streaming_session.py queue-depth fix that counts only in-flight tasks.
    static let chunkMinSpeechMs: Double = 5_000          // earliest a silence may close a chunk (since last cut)
    // chunkHardCapMs is the safety net for monologue speech with no pauses. Forced cuts
    // CAN land mid-word — Mercury rejoins the seam on the joined text. If this shows
    // up as a real artifact in practice, follow-up: track lowest-energy frame in the
    // last N seconds and cut there instead of at sessionElapsedMs.
    static let chunkHardCapMs: Double = 30_000
    static let minChunkFrames: Int = 1_600               // 100 ms at 16 kHz equivalent
    static let minChunkAverageDB: Float = -50            // average RMS gate

    /// Mutable per-session VAD state. Pass by `inout` to `classify(...)`.
    struct State {
        var inSpeech: Bool = false
        /// Rolling per-window dBFS history for the noise-floor calculation.
        /// Capacity = noiseFloorWindowMs / windowMs.
        var rmsHistory: [Float] = []
        /// Total elapsed time spent in silence since the last inSpeech frame.
        /// Used to enforce silenceHangoverMs before declaring a silence boundary.
        var silenceAccumMs: Double = 0
        /// Whether a speech-start event has fired for this session.
        var didFireSpeechStart: Bool = false
    }

    enum CutKind { case silence; case forced }
    enum Event {
        case noOp
        case speechStart(atMs: Double)
        case cut(atMs: Double, kind: CutKind)
    }

    /// Per-30-ms-window RMS classification.
    /// `sessionElapsedMs` is total session time so far (used to enforce the 8s bypass).
    /// `speechSinceLastCutMs` is time spent in speech since the last emitted cut
    /// (used for chunkMinSpeechMs / chunkHardCapMs gates).
    /// Caller is responsible for tracking sessionElapsedMs and speechSinceLastCutMs;
    /// this struct only classifies a single buffer and updates state.
    func classify(
        buffer: AVAudioPCMBuffer,
        state: inout State,
        sessionElapsedMs: Double,
        speechSinceLastCutMs: Double
    ) -> Event {
        // 1. Compute RMS in dBFS for THIS buffer (treat the whole buffer as one window).
        let rmsDB = Self.computeRmsDB(buffer: buffer)

        // 2. Update rolling noise floor: append rmsDB, trim to window size.
        let bufferMs = Double(buffer.frameLength) / buffer.format.sampleRate * 1000.0
        let maxHistory = max(1, Int(Self.noiseFloorWindowMs / max(bufferMs, 1)))
        state.rmsHistory.append(rmsDB)
        if state.rmsHistory.count > maxHistory { state.rmsHistory.removeFirst() }
        let noiseFloor = state.rmsHistory.min() ?? Self.noiseFloorFallbackDB
        let effectiveFloor = max(noiseFloor, Self.noiseFloorFallbackDB)

        // 3. Apply hysteresis.
        let enterThresh = effectiveFloor + Self.speechEnterThresholdAboveNoiseFloorDB
        let exitThresh = effectiveFloor + Self.speechExitThresholdAboveNoiseFloorDB
        let wasInSpeech = state.inSpeech
        if !state.inSpeech && rmsDB > enterThresh {
            state.inSpeech = true
            state.silenceAccumMs = 0
        } else if state.inSpeech && rmsDB < exitThresh {
            state.silenceAccumMs += bufferMs
            if state.silenceAccumMs >= Self.silenceHangoverMs {
                state.inSpeech = false
            }
        } else if state.inSpeech {
            state.silenceAccumMs = 0
        }

        // 4. Emit speechStart on first transition into speech.
        if !wasInSpeech && state.inSpeech && !state.didFireSpeechStart {
            state.didFireSpeechStart = true
            return .speechStart(atMs: sessionElapsedMs)
        }

        // 5. Silence-boundary cut: just exited speech AND past 8s bypass AND past 20s min.
        if wasInSpeech && !state.inSpeech
           && sessionElapsedMs >= Self.minSpeechBeforeStreamMs
           && speechSinceLastCutMs >= Self.chunkMinSpeechMs {
            return .cut(atMs: sessionElapsedMs, kind: .silence)
        }

        // 6. Forced cut at hard cap.
        if speechSinceLastCutMs >= Self.chunkHardCapMs {
            return .cut(atMs: sessionElapsedMs, kind: .forced)
        }

        return .noOp
    }

    /// Compute RMS dBFS for the entire buffer, averaging across channels.
    /// Returns noiseFloorFallbackDB for zero-frame or all-silence buffers.
    static func computeRmsDB(buffer: AVAudioPCMBuffer) -> Float {
        guard let channelData = buffer.floatChannelData else { return noiseFloorFallbackDB }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return noiseFloorFallbackDB }
        let channels = Int(buffer.format.channelCount)
        var sumSquares: Double = 0
        var n: Int = 0
        for ch in 0..<channels {
            let ptr = channelData[ch]
            for i in 0..<frameCount {
                let v = Double(ptr[i])
                sumSquares += v * v
                n += 1
            }
        }
        guard n > 0 else { return noiseFloorFallbackDB }
        let mean = sumSquares / Double(n)
        let rms = sqrt(mean)
        // 20 * log10(rms / 1.0)  — assumes Float32 normalized to ±1.0.
        if rms <= 1e-7 { return noiseFloorFallbackDB }
        return Float(20.0 * log10(rms))
    }

    /// Helper for chunk-emit-time: compute average RMS dBFS over a contiguous
    /// interleaved Float32 sample buffer (used by the recorder's silence guard).
    static func averageRmsDB(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else { return noiseFloorFallbackDB }
        var sumSquares: Double = 0
        for v in samples { sumSquares += Double(v) * Double(v) }
        let mean = sumSquares / Double(samples.count)
        let rms = sqrt(mean)
        if rms <= 1e-7 { return noiseFloorFallbackDB }
        return Float(20.0 * log10(rms))
    }
}
