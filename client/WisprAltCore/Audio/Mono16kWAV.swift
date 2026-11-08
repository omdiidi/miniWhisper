import AVFoundation
import Foundation

/// 16 kHz mono 16-bit PCM WAV, the upload format for every recorded OpenAI
/// request (600 s ≈ 19.2 MB, under the 25 MB limit).
public enum Mono16kWAV {
    public static let sampleRate = 16_000
    private static let blockFrames: AVAudioFrameCount = 32_768

    public enum Failure: LogSafeError, Equatable {
        case unreadable
        public var logDescription: String { "mono16k(unreadable)" }
        public var outcomeKind: ErrorOutcome { .failed(.encodeFailed) }
    }

    /// Reads any file `AVAudioFile` can open, averages its channels, resamples
    /// to 16 kHz and returns a WAV. `range` (seconds) limits the part read.
    public static func make(from url: URL, range: Range<Double>? = nil) throws -> Data {
        var resampler: MonoResampler?
        var pcm = Data()
        try readBlocks(url, range: range) { format, interleaved in
            let r = resampler ?? MonoResampler(inputRate: format.sampleRate,
                                               channels: Int(format.channelCount),
                                               outputRate: Double(sampleRate))
            resampler = r
            appendInt16(r.process(interleaved), to: &pcm)
        }
        if let resampler { appendInt16(resampler.flush(), to: &pcm) }
        return header(dataBytes: pcm.count) + pcm
    }

    /// RMS of the mono (channel-averaged) signal per `windowMs` window, at the
    /// file's own rate. Used to find quiet places to cut long audio.
    public static func rmsProfile(from url: URL, windowMs: Int) throws -> [Float] {
        var out: [Float] = []
        var sum: Float = 0
        var count = 0
        try readBlocks(url, range: nil) { format, interleaved in
            let channels = Int(format.channelCount)
            let window = max(1, Int(format.sampleRate * Double(windowMs) / 1000))
            for f in 0..<(interleaved.count / channels) {
                var s: Float = 0
                for c in 0..<channels { s += interleaved[f * channels + c] }
                s /= Float(channels)
                sum += s * s
                count += 1
                if count == window { out.append((sum / Float(count)).squareRoot()); sum = 0; count = 0 }
            }
        }
        if count > 0 { out.append((sum / Float(count)).squareRoot()) }
        return out
    }

    /// WAV bytes for mono Float samples in [-1, 1] at `sampleRate`.
    public static func encode(_ mono: [Float]) -> Data {
        var pcm = Data()
        appendInt16(mono, to: &pcm)
        return header(dataBytes: pcm.count) + pcm
    }

    // MARK: - Private

    /// Reads `url` in blocks of interleaved Float32 frames.
    private static func readBlocks(_ url: URL, range: Range<Double>?, _ body: (AVAudioFormat, [Float]) -> Void) throws {
        guard let file = try? AVAudioFile(forReading: url) else { throw Failure.unreadable }
        let format = file.processingFormat
        let channels = Int(format.channelCount)
        var endFrame = file.length
        if let range {
            file.framePosition = AVAudioFramePosition(range.lowerBound * format.sampleRate)
            endFrame = min(endFrame, AVAudioFramePosition(range.upperBound * format.sampleRate))
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: blockFrames) else {
            throw Failure.unreadable
        }
        var interleaved = [Float]()
        while file.framePosition < endFrame {
            let want = AVAudioFrameCount(min(AVAudioFramePosition(blockFrames), endFrame - file.framePosition))
            do { try file.read(into: buffer, frameCount: want) } catch { throw Failure.unreadable }
            let frames = Int(buffer.frameLength)
            guard frames > 0, let data = buffer.floatChannelData else { break }
            interleaved.removeAll(keepingCapacity: true)
            interleaved.reserveCapacity(frames * channels)
            for f in 0..<frames {
                for c in 0..<channels { interleaved.append(data[c][f]) }
            }
            body(format, interleaved)
        }
    }

    private static func appendInt16(_ samples: [Float], to data: inout Data) {
        var ints = [Int16](repeating: 0, count: samples.count)
        for i in 0..<samples.count {
            ints[i] = Int16((max(-1, min(1, samples[i])) * 32767).rounded())
        }
        ints.withUnsafeBytes { data.append(contentsOf: $0) }   // little-endian on every Mac
    }

    private static func header(dataBytes: Int) -> Data {
        var h = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { h.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { h.append(contentsOf: $0) } }
        h.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + dataBytes))
        h.append(contentsOf: Array("WAVE".utf8))
        h.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1)
        u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
        h.append(contentsOf: Array("data".utf8)); u32(UInt32(dataBytes))
        return h
    }
}
