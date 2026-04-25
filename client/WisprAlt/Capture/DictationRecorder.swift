import AVFoundation
import Foundation

// MARK: - DictationRecorder

/// Records microphone audio via `AVAudioEngine` and produces a 16 kHz mono Float32 WAV
/// suitable for submission to the `/transcribe/dictate` endpoint.
///
/// ## Mic mutual exclusion
/// `start()` returns `false` if `MeetingRecorder.shared.isActive` is `true`, because
/// both recorders cannot share the input node simultaneously. The caller should present
/// a brief UI toast explaining the no-op.
///
/// ## Lifetime
/// Typical usage: create once; call `start()` on FN key-down, `stop()` on FN key-up.
/// `stop()` is `async` to ensure the AVAudioEngine has fully flushed before the WAV
/// bytes are returned.
final class DictationRecorder {

    // MARK: - Errors

    enum DictationError: Error, LocalizedError {
        case meetingRecordingActive
        case engineStartFailed(Error)
        case notRecording

        var errorDescription: String? {
            switch self {
            case .meetingRecordingActive:
                return "Cannot start dictation while meeting recording is active."
            case .engineStartFailed(let err):
                return "AVAudioEngine failed to start: \(err.localizedDescription)"
            case .notRecording:
                return "DictationRecorder is not currently recording."
            }
        }
    }

    // MARK: - State

    private let engine = AVAudioEngine()

    /// Retained AVAudioConverter to avoid per-buffer allocation (mirrors v3 P4#8 intent).
    private var converter: AVAudioConverter?

    /// Accumulated Float32 samples at 16 kHz.
    private var samples: [Float] = []

    private var isRecording = false

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

        samples.removeAll(keepingCapacity: true)

        let inputNode = engine.inputNode
        let inputFormat = inputNode.outputFormat(forBus: 0)

        // Build or verify the retained converter.
        if converter == nil || converter?.inputFormat != inputFormat {
            guard let conv = AVAudioConverter(
                from: inputFormat,
                to: AudioFormat.canonical16kFloat32Mono
            ) else {
                Log.error("DictationRecorder: failed to create AVAudioConverter", category: "capture")
                throw DictationError.engineStartFailed(
                    NSError(domain: "co.wispralt", code: -1,
                            userInfo: [NSLocalizedDescriptionKey: "AVAudioConverter init failed"])
                )
            }
            converter = conv
            Log.debug("DictationRecorder: converter created (\(inputFormat.sampleRate) Hz \(inputFormat.channelCount)ch → 16k mono)", category: "capture")
        }

        let convRef = converter! // safe: just assigned or was already set

        // Install tap on the input node using its native format.
        // Buffer size 4096 frames gives ~85 ms at 48 kHz (common macOS default).
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            self.processTapBuffer(buffer, converter: convRef)
        }

        do {
            try engine.start()
        } catch {
            inputNode.removeTap(onBus: 0)
            converter = nil
            throw DictationError.engineStartFailed(error)
        }

        isRecording = true
        Log.info("DictationRecorder: recording started.", category: "capture")
        return true
    }

    /// Stops capture, encodes accumulated samples as a 16 kHz mono Float32 WAV, and
    /// returns the encoded bytes.
    ///
    /// The WAV encoding uses `AVAudioFile` writing to a temporary URL on the user's
    /// temp directory, then reads the bytes back.  This is simpler and less error-prone
    /// than manually constructing the 44-byte WAV header, and AVAudioFile guarantees
    /// correct RIFF/WAVE header writing including the data-chunk size.
    ///
    /// - Returns: WAV-encoded `Data`.
    /// - Throws: `DictationError.notRecording` if `start()` was never called.
    func stop() async throws -> Data {
        guard isRecording else {
            throw DictationError.notRecording
        }

        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        isRecording = false

        let captured = samples
        samples.removeAll(keepingCapacity: false)

        Log.info("DictationRecorder: stopped, \(captured.count) samples at 16kHz.", category: "capture")

        return try await Task.detached(priority: .userInitiated) {
            try encodeAsWAV(samples: captured)
        }.value
    }

    // MARK: - Tap callback (called on AVAudioEngine's internal thread)

    private func processTapBuffer(_ buffer: AVAudioPCMBuffer, converter: AVAudioConverter) {
        let ratio = AudioFormat.canonical16kFloat32Mono.sampleRate / buffer.format.sampleRate
        let outCapacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1

        guard let outBuf = AVAudioPCMBuffer(
            pcmFormat: AudioFormat.canonical16kFloat32Mono,
            frameCapacity: max(outCapacity, 1)
        ) else { return }

        var inputConsumed = false
        var convError: NSError?
        let status = converter.convert(to: outBuf, error: &convError) { _, outStatus in
            if inputConsumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            inputConsumed = true
            outStatus.pointee = .haveData
            return buffer
        }

        guard status != .error,
              outBuf.frameLength > 0,
              let channelData = outBuf.floatChannelData?[0] else { return }

        let newSamples = Array(UnsafeBufferPointer(start: channelData, count: Int(outBuf.frameLength)))
        samples.append(contentsOf: newSamples)
    }
}

// MARK: - WAV encoding helper (free function)

/// Writes `samples` (16 kHz Float32 mono) to a temporary file using `AVAudioFile`
/// and returns the resulting bytes.
///
/// `AVAudioFile` writes a standard RIFF/WAVE header automatically. The output is
/// little-endian Float32 PCM (standard WAV type 3), which the server's
/// `soundfile.read` handles correctly.
private func encodeAsWAV(samples: [Float]) throws -> Data {
    let tempURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("wispralt_dictation_\(UUID().uuidString).wav")

    defer { try? FileManager.default.removeItem(at: tempURL) }

    guard let outBuf = AVAudioPCMBuffer(
        pcmFormat: AudioFormat.canonical16kFloat32Mono,
        frameCapacity: AVAudioFrameCount(samples.count)
    ) else {
        throw NSError(domain: "co.wispralt", code: -2,
                      userInfo: [NSLocalizedDescriptionKey: "Failed to allocate PCM buffer for WAV encode"])
    }
    outBuf.frameLength = AVAudioFrameCount(samples.count)

    if let dst = outBuf.floatChannelData?[0] {
        samples.withUnsafeBufferPointer { src in
            memcpy(dst, src.baseAddress!, samples.count * MemoryLayout<Float>.size)
        }
    }

    let audioFile = try AVAudioFile(
        forWriting: tempURL,
        settings: AudioFormat.canonical16kFloat32Mono.settings,
        commonFormat: .pcmFormatFloat32,
        interleaved: false
    )
    try audioFile.write(from: outBuf)

    return try Data(contentsOf: tempURL)
}
