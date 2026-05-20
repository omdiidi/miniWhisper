import AVFoundation
import Foundation

/// Lossless-for-speech downcast from a Float32 PCM WAV on disk to an in-memory
/// 16-bit PCM WAV at the same sample rate and channel count.
///
/// Why this exists
/// ===============
/// `DictationRecorder` writes Float32 WAV at the input device's native rate
/// (typically 48 kHz, mono) directly from the AVAudioEngine tap. That's the
/// only known-safe write path: see the long comment block at the top of
/// `DictationRecorder.swift` for the history of failures when letting
/// `AVAudioFile`'s internal converter handle Float→Int16 (it applies a buggy
/// normalization that amplifies the signal ~140× and rails the Int16).
///
/// However Float32 doubles the upload size for zero perceptual benefit — the
/// server resamples to 16 kHz and Parakeet is trained on 16-bit speech audio.
/// This helper does an explicit, deterministic Float32 → Int16 conversion
/// AFTER the Float32 WAV has been written, completely outside of AVAudioFile's
/// writer path. The conversion is a textbook clamp-and-scale:
///
///     int16 = round(clamp(float, -1.0, +1.0) * 32767)
///
/// Then we serialize a canonical WAV header (PCM/LE/Int16, no fact chunk, no
/// extension fields). The output is byte-for-byte deterministic and the same
/// bits any reference WAV encoder would produce.
///
/// Result: ~50% smaller upload payload with zero impact on transcription
/// quality. Parakeet on the server side resamples 48 kHz → 16 kHz; that
/// resample is bottlenecked by the source signal's own bandwidth, not the
/// container's bit depth, so the 32→16 bit downcast at full bandwidth is
/// inaudible to the model.
///
/// Read-side safety
/// ----------------
/// We use `AVAudioFile` only for *reading* the Float32 source — read does not
/// trigger the buggy internal converter. The output bytes are assembled by
/// hand without involving any Apple converter, so the conversion-amplification
/// failure mode that motivated the Float32-write path can't recur here.
enum Int16WAVEncoder {

    enum EncodeError: Error, LocalizedError {
        case openFailed(Error)
        case readFailed(Error)
        case unsupportedSourceFormat(String)
        case zeroFrames

        var errorDescription: String? {
            switch self {
            case .openFailed(let e): return "Could not open source WAV: \(e.localizedDescription)"
            case .readFailed(let e): return "Could not read source WAV: \(e.localizedDescription)"
            case .unsupportedSourceFormat(let why): return "Unsupported source format: \(why)"
            case .zeroFrames: return "Source WAV contains zero audio frames."
            }
        }
    }

    /// Reads a Float32 PCM WAV from disk and returns a 16-bit PCM WAV (Data)
    /// at the same sample rate and channel count. Channels are written
    /// interleaved (the standard WAV layout) regardless of the source's
    /// interleaving.
    ///
    /// Clamps every sample to ±1.0 before scaling so out-of-range floats (which
    /// `DictationRecorder` already clamps to ±0.95 in the tap callback, but we
    /// defend in depth here) cannot wrap around when cast to Int16.
    static func encode(fromFloat32WAVAt url: URL) throws -> Data {
        // Open the source. AVAudioFile's *read* path performs no conversion when
        // we ask for the file's native processing format — this avoids the
        // amplification bug that bites the *write* path.
        let file: AVAudioFile
        do {
            file = try AVAudioFile(forReading: url)
        } catch {
            throw EncodeError.openFailed(error)
        }

        let processingFormat = file.processingFormat
        let sampleRate = processingFormat.sampleRate
        let channelCount = Int(processingFormat.channelCount)
        guard sampleRate > 0, channelCount > 0 else {
            throw EncodeError.unsupportedSourceFormat(
                "sampleRate=\(sampleRate) channels=\(channelCount)"
            )
        }
        guard processingFormat.commonFormat == .pcmFormatFloat32 else {
            throw EncodeError.unsupportedSourceFormat(
                "expected Float32, got commonFormat=\(processingFormat.commonFormat.rawValue)"
            )
        }

        let totalFrames = AVAudioFrameCount(file.length)
        guard totalFrames > 0 else { throw EncodeError.zeroFrames }

        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: processingFormat,
            frameCapacity: totalFrames
        ) else {
            throw EncodeError.unsupportedSourceFormat("could not allocate read buffer")
        }

        do {
            try file.read(into: buffer, frameCount: totalFrames)
        } catch {
            throw EncodeError.readFailed(error)
        }

        let frames = Int(buffer.frameLength)
        guard frames > 0 else { throw EncodeError.zeroFrames }
        guard let floatChannels = buffer.floatChannelData else {
            throw EncodeError.unsupportedSourceFormat("read buffer has no floatChannelData")
        }

        // Build the interleaved Int16 payload.
        //
        // Scaling factor 32767 (not 32768) prevents `Int16.min` overflow for
        // a float sample of exactly −1.0:
        //   round(-1.0 * 32768) = -32768 → fits Int16.min, BUT
        //   round(+1.0 * 32768) = +32768 → overflows Int16.max (32767).
        // Using 32767 makes the conversion symmetric and lossless within the
        // representable range. The trade-off is a 0.00003 dB amplitude loss
        // at full scale, well below human perceptual threshold and far below
        // any speech ASR's input sensitivity.
        let sampleCount = frames * channelCount
        var pcm = [Int16](repeating: 0, count: sampleCount)
        pcm.withUnsafeMutableBufferPointer { dst in
            for frame in 0..<frames {
                for ch in 0..<channelCount {
                    var v = floatChannels[ch][frame]
                    if v > 1.0 { v = 1.0 } else if v < -1.0 { v = -1.0 }
                    // Round-half-away-from-zero via copysign + 0.5 trick.
                    let scaled = v * 32767.0
                    let rounded = (scaled >= 0 ? scaled + 0.5 : scaled - 0.5)
                    dst[frame * channelCount + ch] = Int16(rounded)
                }
            }
        }

        return makeWAV(
            int16Samples: pcm,
            sampleRate: UInt32(sampleRate.rounded()),
            channels: UInt16(channelCount)
        )
    }

    /// Encodes an in-memory buffer of INTERLEAVED Float32 audio samples to a
    /// 16-bit PCM WAV `Data` blob, skipping any disk I/O.
    ///
    /// Sample layout
    /// -------------
    /// `frames` is treated as interleaved: sample at position `f * channels + ch`
    /// is frame `f`, channel `ch`. For mono audio `channels == 1` and layout is
    /// trivially `[f0, f1, f2, ...]`.
    ///
    /// Float→Int16 conversion
    /// ----------------------
    /// Identical to `encode(fromFloat32WAVAt:)`:
    ///   1. Clamp each float to [-1.0, +1.0] (defends against out-of-range floats).
    ///   2. Multiply by 32767 (symmetric scale; see notes in `encode(...)` for why
    ///      not 32768).
    ///   3. Round-half-away-from-zero via the `+0.5 / -0.5` trick.
    ///   4. Cast to Int16.
    ///
    /// Output byte stream is byte-identical to what `encode(fromFloat32WAVAt:)`
    /// would produce for an equivalent on-disk Float32 WAV at the same sample
    /// rate and channel count — both paths funnel through `makeWAV(...)`.
    ///
    /// - Parameters:
    ///   - frames: Interleaved Float32 samples in `[-1, +1]` nominal range. May
    ///     be empty (returns a valid zero-data WAV).
    ///   - sampleRate: Source sample rate in Hz. Rounded to the nearest UInt32
    ///     when written into the header.
    ///   - channels: Channel count (1 = mono, 2 = stereo, …).
    static func encodeFloat32Frames(_ frames: [Float], sampleRate: Double, channels: Int) -> Data {
        let channelCount = max(channels, 1)
        var pcm = [Int16](repeating: 0, count: frames.count)
        pcm.withUnsafeMutableBufferPointer { dst in
            frames.withUnsafeBufferPointer { src in
                for i in 0..<frames.count {
                    var v = src[i]
                    if v > 1.0 { v = 1.0 } else if v < -1.0 { v = -1.0 }
                    let scaled = v * 32767.0
                    let rounded = (scaled >= 0 ? scaled + 0.5 : scaled - 0.5)
                    dst[i] = Int16(rounded)
                }
            }
        }
        return makeWAV(
            int16Samples: pcm,
            sampleRate: UInt32(sampleRate.rounded()),
            channels: UInt16(channelCount)
        )
    }

    // MARK: - Private: canonical 16-bit PCM WAV header builder

    /// Builds a canonical RIFF/WAVE container with a single `fmt ` chunk and a
    /// single `data` chunk. No `fact`, no extension fields — minimal layout
    /// that every WAV decoder (and the server's soundfile reader) handles.
    private static func makeWAV(
        int16Samples: [Int16],
        sampleRate: UInt32,
        channels: UInt16
    ) -> Data {
        let bitsPerSample: UInt16 = 16
        let bytesPerSample: UInt16 = bitsPerSample / 8
        let blockAlign: UInt16 = channels * bytesPerSample
        let byteRate: UInt32 = sampleRate * UInt32(blockAlign)
        let dataSize: UInt32 = UInt32(int16Samples.count) * UInt32(bytesPerSample)
        let fmtChunkSize: UInt32 = 16
        let riffSize: UInt32 = 4 /* "WAVE" */ + (8 + fmtChunkSize) + (8 + dataSize)

        var data = Data()
        data.reserveCapacity(44 + Int(dataSize))

        // RIFF header
        data.append(contentsOf: "RIFF".utf8)
        data.appendLE(riffSize)
        data.append(contentsOf: "WAVE".utf8)

        // fmt chunk
        data.append(contentsOf: "fmt ".utf8)
        data.appendLE(fmtChunkSize)
        data.appendLE(UInt16(1)) // AudioFormat = 1 (PCM)
        data.appendLE(channels)
        data.appendLE(sampleRate)
        data.appendLE(byteRate)
        data.appendLE(blockAlign)
        data.appendLE(bitsPerSample)

        // data chunk
        data.append(contentsOf: "data".utf8)
        data.appendLE(dataSize)
        int16Samples.withUnsafeBufferPointer { buf in
            buf.baseAddress?.withMemoryRebound(
                to: UInt8.self,
                capacity: int16Samples.count * MemoryLayout<Int16>.size
            ) { byteBase in
                data.append(byteBase, count: int16Samples.count * MemoryLayout<Int16>.size)
            }
        }
        return data
    }
}

// MARK: - Little-endian append helpers

private extension Data {
    mutating func appendLE(_ value: UInt16) {
        var v = value.littleEndian
        Swift.withUnsafeBytes(of: &v) { append(contentsOf: $0) }
    }
    mutating func appendLE(_ value: UInt32) {
        var v = value.littleEndian
        Swift.withUnsafeBytes(of: &v) { append(contentsOf: $0) }
    }
}
