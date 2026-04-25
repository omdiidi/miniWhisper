import AVFoundation
import CoreMedia

// MARK: - Canonical audio formats

/// Shared canonical AVAudioFormat constants used throughout the capture pipeline.
enum AudioFormat {
    /// 16 kHz, 1-channel (mono), non-interleaved Float32.
    /// Target format for dictation capture and the mic channel in meeting capture.
    static let canonical16kFloat32Mono: AVAudioFormat = {
        guard let fmt = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: 16_000,
            channels: 1,
            interleaved: false
        ) else { fatalError("Failed to create canonical16kFloat32Mono") }
        return fmt
    }()

    /// 16 kHz, 2-channel (stereo), non-interleaved Float32.
    /// The output format written by MeetingRecorder: ch[0] = mic, ch[1] = system audio.
    static let canonical16kFloat32Stereo: AVAudioFormat = {
        guard let fmt = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: 16_000,
            channels: 2,
            interleaved: false
        ) else { fatalError("Failed to create canonical16kFloat32Stereo") }
        return fmt
    }()
}

// MARK: - CMSampleBufferConverter

/// Converts `CMSampleBuffer` objects to 16 kHz mono Float32 `AVAudioPCMBuffer`.
///
/// **Thread safety**: instances are NOT thread-safe. Each audio source (mic or system)
/// should own one dedicated instance, called exclusively from a single serial queue.
///
/// **Converter retention (v3 P4#8)**: the `AVAudioConverter` is created once on the
/// first buffer and reused for the lifetime of the instance. This avoids the
/// per-call allocation overhead and preserves the stateful resampler's internal state,
/// which is important for correct sample-rate conversion across buffer boundaries.
final class CMSampleBufferConverter {

    // MARK: - Retained state

    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?

    // MARK: - Public API

    /// Converts `sampleBuffer` to `AudioFormat.canonical16kFloat32Mono`.
    ///
    /// Returns `nil` if:
    /// - The sample buffer has no format description.
    /// - The converter cannot be created from the source format.
    /// - The conversion produces zero frames (e.g. `.noDataNow` on the pull callback).
    func convertTo16kMono(_ sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        // Lazily build converter on first buffer, then reuse.
        if converter == nil {
            guard let desc = CMSampleBufferGetFormatDescription(sampleBuffer),
                  let src = AVAudioFormat(cmAudioFormatDescription: desc) else {
                Log.error("CMSampleBufferConverter: missing format description", category: "capture")
                return nil
            }
            guard let conv = AVAudioConverter(from: src, to: AudioFormat.canonical16kFloat32Mono) else {
                Log.error("CMSampleBufferConverter: failed to create AVAudioConverter from \(src) to 16k mono", category: "capture")
                return nil
            }
            sourceFormat = src
            converter = conv
            Log.debug("CMSampleBufferConverter: converter created (\(src.sampleRate) Hz \(src.channelCount)ch → 16k mono)", category: "capture")
        }

        guard let conv = converter,
              let src = sourceFormat else { return nil }

        // Wrap the CMSampleBuffer as an AVAudioPCMBuffer for the pull callback.
        guard let inputPCM = pcmBuffer(from: sampleBuffer, format: src) else {
            return nil
        }

        // Calculate output frame capacity based on ratio.
        let ratio = AudioFormat.canonical16kFloat32Mono.sampleRate / src.sampleRate
        let outCapacity = AVAudioFrameCount(ceil(Double(inputPCM.frameLength) * ratio)) + 1

        guard let outputBuf = AVAudioPCMBuffer(
            pcmFormat: AudioFormat.canonical16kFloat32Mono,
            frameCapacity: max(outCapacity, 1)
        ) else { return nil }

        // Stateful pull-callback conversion.
        var inputConsumed = false
        var convError: NSError?
        let status = conv.convert(to: outputBuf, error: &convError) { _, outStatus in
            if inputConsumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            inputConsumed = true
            outStatus.pointee = .haveData
            return inputPCM
        }

        if let err = convError {
            Log.error("CMSampleBufferConverter: conversion error \(err)", category: "capture")
            return nil
        }

        guard status != .error, outputBuf.frameLength > 0 else {
            return nil
        }

        return outputBuf
    }

    // MARK: - Private helpers

    /// Wraps a `CMSampleBuffer` into an `AVAudioPCMBuffer` without copying audio data
    /// where possible.  Falls back to a manual copy if the block list isn't directly
    /// mappable (e.g. non-contiguous).
    private func pcmBuffer(from sampleBuffer: CMSampleBuffer, format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0 else { return nil }

        guard let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else { return nil }
        pcm.frameLength = frameCount

        // Copy audio data from the CMBlockBuffer.
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return nil }
        var dataLength = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(
            blockBuffer,
            atOffset: 0,
            lengthAtOffsetOut: nil,
            totalLengthOut: &dataLength,
            dataPointerOut: &dataPointer
        )
        guard status == kCMBlockBufferNoErr, let src = dataPointer else { return nil }

        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        let channelCount = Int(format.channelCount)

        if format.isInterleaved {
            // Interleaved: single flat buffer.
            guard let dst = pcm.floatChannelData?[0] else { return nil }
            memcpy(dst, src, min(dataLength, Int(frameCount) * bytesPerFrame * channelCount))
        } else {
            // Non-interleaved: de-interleave channel-by-channel.
            // SCStream typically delivers interleaved PCM even when we request non-interleaved;
            // perform manual de-interleave.
            let srcFloats = UnsafeBufferPointer<Float>(
                start: UnsafeRawPointer(src).bindMemory(to: Float.self, capacity: Int(frameCount) * channelCount),
                count: Int(frameCount) * channelCount
            )
            for ch in 0..<channelCount {
                guard let dst = pcm.floatChannelData?[ch] else { continue }
                for frame in 0..<Int(frameCount) {
                    dst[frame] = srcFloats[frame * channelCount + ch]
                }
            }
        }

        return pcm
    }
}
