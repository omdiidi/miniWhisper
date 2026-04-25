import AVFoundation

// MARK: - AudioChannel

/// Identifies which capture stream a buffer belongs to.
enum AudioChannel {
    case mic
    case system
}

// MARK: - AlignedRingBuffer

/// Sample-position-keyed ring buffer that aligns mic and system audio streams and
/// flushes them as interleaved stereo chunks.
///
/// ## Design
/// Each channel maintains a sorted array of `Chunk` values keyed by their absolute
/// sample position (in 16kHz samples, measured from recording start). The buffer
/// tracks a `committed` cursor: the highest sample position that has already been
/// written to the output file. `flushAligned()` advances the cursor whenever both
/// channels have data covering the next range.
///
/// ## Thread safety
/// All public methods are protected by `os_unfair_lock`. In `MeetingRecorder`,
/// all `append` and `flush` calls originate from the single serial `ioQueue`, so
/// the lock is lightly contended and almost never actually blocks.
///
/// ## Gap tolerance
/// A 200 ms tolerance (`gapToleranceSamples` = 3200 at 16 kHz) is applied:
/// if one channel is ahead of the other by more than this, `flushAligned(forceFlush:)`
/// pads the lagging channel with silence rather than waiting indefinitely.
///
/// ## Wall-clock stall detection
/// `MeetingRecorder` runs a 100 ms `DispatchSourceTimer` that calls
/// `forceFlushIfStalled(now:)`. If a channel has been silent (no new append) for
/// longer than `gapToleranceSamples` / 16000 seconds while the other channel has
/// queued data, the stalled channel is padded and flushed.
final class AlignedRingBuffer {

    // MARK: - Internal chunk type

    private struct Chunk {
        let start: Int
        let buf: AVAudioPCMBuffer

        var end: Int { start + Int(buf.frameLength) }
    }

    // MARK: - State

    private var mic: [Chunk] = []
    private var sys: [Chunk] = []

    /// The sample position up to which data has been committed (written to file).
    private var committed: Int = 0

    private var lock = os_unfair_lock_s()

    // MARK: - Constants

    /// 200 ms at 16 kHz = 3200 samples.
    private let gapToleranceSamples: Int = 16_000 / 5

    // MARK: - Wall-clock stall detection

    /// Monotonic time (seconds) of the last `append` call for each channel.
    private var lastMicAppendWallTime: Double = 0
    private var lastSysAppendWallTime: Double = 0

    // MARK: - Public API

    /// Inserts `buf` into the sorted per-channel queue at absolute sample position `atSamplePos`.
    ///
    /// - Parameter buf: A 16 kHz mono Float32 buffer.
    /// - Parameter atSamplePos: Absolute sample offset from recording start.
    ///   Negative values are clamped to 0 (v3 P4#2).
    /// - Parameter channel: Which capture stream this buffer belongs to.
    func append(_ buf: AVAudioPCMBuffer, atSamplePos: Int, channel: AudioChannel) {
        guard buf.frameLength > 0 else { return }

        let safe = max(0, atSamplePos)  // v3 P4#2: clamp negative offsets from PTS race
        let chunk = Chunk(start: safe, buf: buf)
        let now = Date().timeIntervalSinceReferenceDate

        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }

        switch channel {
        case .mic:
            binaryInsert(chunk, into: &mic)
            lastMicAppendWallTime = now
        case .system:
            binaryInsert(chunk, into: &sys)
            lastSysAppendWallTime = now
        }
    }

    /// Drains aligned samples from both channels up to the minimum of each channel's
    /// next-available head end position.
    ///
    /// Returns a 2-channel non-interleaved `AVAudioPCMBuffer` in
    /// `AudioFormat.canonical16kFloat32Stereo`, or `nil` if nothing is ready.
    ///
    /// - Parameter forceFlush: When `true`, the lagging channel is padded with silence
    ///   (zeros) rather than waiting for it to catch up. Used on `stop()` and by the
    ///   wall-clock stall timer.
    func flushAligned(forceFlush: Bool = false) -> AVAudioPCMBuffer? {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }

        return _flushAligned(forceFlush: forceFlush)
    }

    /// Wall-clock fallback called every 100 ms by `MeetingRecorder`'s flush timer.
    ///
    /// If a channel has been silent (no new `append`) for more than `gapToleranceSamples`
    /// / 16000 seconds while the other has queued data, pads the silent channel and
    /// returns `true` so the caller knows to drain via `flushAligned()`.
    ///
    /// - Parameter now: The current wall-clock time (`Date().timeIntervalSinceReferenceDate`).
    /// - Returns: `true` if a force-flush was triggered.
    func forceFlushIfStalled(now: Double) -> Bool {
        os_unfair_lock_lock(&lock)
        let gapSeconds = Double(gapToleranceSamples) / 16_000.0
        let micStale = (now - lastMicAppendWallTime) > gapSeconds && !sys.isEmpty && mic.isEmpty
        let sysStale = (now - lastSysAppendWallTime) > gapSeconds && !mic.isEmpty && sys.isEmpty
        os_unfair_lock_unlock(&lock)

        return micStale || sysStale
    }

    /// Pads the lagging channel with silence up to the longest tail of either channel,
    /// so that `flushAligned(forceFlush: true)` can drain all remaining data.
    ///
    /// Call this once after stopping capture, before the final drain loop.
    func padMissing(toEnd: Bool) {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }

        guard toEnd else { return }

        let micTail = mic.last?.end ?? committed
        let sysTail = sys.last?.end ?? committed
        let target = max(micTail, sysTail)

        if micTail < target {
            silencePad(into: &mic, from: micTail, to: target)
        }
        if sysTail < target {
            silencePad(into: &sys, from: sysTail, to: target)
        }
    }

    // MARK: - Internal flush (must be called under lock)

    private func _flushAligned(forceFlush: Bool) -> AVAudioPCMBuffer? {
        // Step 1: find the "end" position of the first chunk in each channel.
        // If a channel has no data, use committedCursor as its end.
        let micEnd = mic.first?.end ?? committed
        let sysEnd = sys.first?.end ?? committed

        // Step 2: target = min of both ends — the smallest range both channels cover.
        let target = min(micEnd, sysEnd)

        // Step 3: nothing to flush if target hasn't advanced past the cursor.
        guard target > committed else { return nil }

        let frameCount = target - committed

        // Step 4: verify both channels have data covering [committed, target).
        // If a channel is missing coverage and forceFlush is false, wait.
        let micCovers = chunkCovers(mic, from: committed, to: target)
        let sysCovers = chunkCovers(sys, from: committed, to: target)

        if !micCovers || !sysCovers {
            guard forceFlush else { return nil }
            // Under force-flush, pad the missing range with silence.
            if !micCovers { silencePad(into: &mic, from: committed, to: target) }
            if !sysCovers { silencePad(into: &sys, from: committed, to: target) }
        }

        // Step 5: build the output buffer.
        guard let output = AVAudioPCMBuffer(
            pcmFormat: AudioFormat.canonical16kFloat32Stereo,
            frameCapacity: AVAudioFrameCount(frameCount)
        ) else { return nil }
        output.frameLength = AVAudioFrameCount(frameCount)

        guard let ch0 = output.floatChannelData?[0],
              let ch1 = output.floatChannelData?[1] else { return nil }

        // Fill channel 0 from mic, channel 1 from system.
        fillChannel(from: &mic, into: ch0, start: committed, end: target)
        fillChannel(from: &sys, into: ch1, start: committed, end: target)

        // Step 6: advance committed cursor.
        committed = target

        // Step 7: drop fully consumed chunks.
        mic.removeAll { $0.end <= committed }
        sys.removeAll { $0.end <= committed }

        return output
    }

    // MARK: - Private helpers

    /// Binary-inserts `chunk` into `array`, keeping it sorted by `start`.
    private func binaryInsert(_ chunk: Chunk, into array: inout [Chunk]) {
        var lo = 0
        var hi = array.count
        while lo < hi {
            let mid = (lo + hi) / 2
            if array[mid].start < chunk.start {
                lo = mid + 1
            } else {
                hi = mid
            }
        }
        array.insert(chunk, at: lo)
    }

    /// Returns true if the chunks in `queue` collectively cover all of `[from, to)`.
    private func chunkCovers(_ queue: [Chunk], from: Int, to: Int) -> Bool {
        var cursor = from
        for chunk in queue {
            if chunk.start > cursor { return false }
            if chunk.end > cursor { cursor = chunk.end }
            if cursor >= to { return true }
        }
        return cursor >= to
    }

    /// Appends a silence chunk covering `[from, to)` to `queue`.
    private func silencePad(into queue: inout [Chunk], from: Int, to: Int) {
        let frameCount = AVAudioFrameCount(to - from)
        guard frameCount > 0,
              let silenceBuf = AVAudioPCMBuffer(
                  pcmFormat: AudioFormat.canonical16kFloat32Mono,
                  frameCapacity: frameCount
              ) else { return }
        silenceBuf.frameLength = frameCount
        // Float buffers are zero-initialised by AVAudioPCMBuffer, so no explicit memset needed.
        let chunk = Chunk(start: from, buf: silenceBuf)
        binaryInsert(chunk, into: &queue)
    }

    /// Copies samples from the sorted `queue` covering `[start, end)` into the
    /// pre-allocated Float output pointer. Fills gaps (holes) with zeros.
    private func fillChannel(
        from queue: inout [Chunk],
        into dst: UnsafeMutablePointer<Float>,
        start: Int,
        end: Int
    ) {
        var writePos = start

        for chunk in queue {
            guard chunk.start < end, chunk.end > start else { continue }

            // Gap before this chunk: fill with silence.
            if chunk.start > writePos {
                let gapSamples = min(chunk.start, end) - writePos
                memset(dst.advanced(by: writePos - start), 0, gapSamples * MemoryLayout<Float>.size)
                writePos += gapSamples
            }

            // Overlap region: copy from chunk.
            let copyStart = max(chunk.start, writePos)
            let copyEnd = min(chunk.end, end)
            if copyEnd > copyStart {
                let chunkOffset = copyStart - chunk.start
                let count = copyEnd - copyStart

                // Access the chunk's Float channel data (mono: channel 0).
                if let srcBase = chunk.buf.floatChannelData?[0] {
                    let src = srcBase.advanced(by: chunkOffset)
                    memcpy(dst.advanced(by: copyStart - start), src, count * MemoryLayout<Float>.size)
                }
                writePos = copyEnd
            }

            if writePos >= end { break }
        }

        // Trailing gap: fill remaining with silence.
        if writePos < end {
            let remaining = end - writePos
            memset(dst.advanced(by: writePos - start), 0, remaining * MemoryLayout<Float>.size)
        }
    }
}
