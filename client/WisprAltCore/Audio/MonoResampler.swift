import Foundation

/// Streaming downmix + resample to mono at `outputRate`.
///
/// Channels are AVERAGED (never summed: `AVAudioConverter`'s default channel
/// mix sums them, which produced the peak-3.97 bug). Resampling uses a
/// windowed-sinc low-pass from a precomputed table. Filter history and the
/// fractional read position carry across `process` calls, so feeding audio in
/// chunks gives the same output as feeding it at once. Call `flush()` once at
/// the end to emit the samples the filter was still holding back.
public final class MonoResampler {
    public let inputRate: Double
    public let outputRate: Double
    public let channels: Int

    private let step: Double            // input samples per output sample
    private let halfWidth: Int          // taps on each side of the read position
    private static let phases = 512
    private let table: [Float]          // (phases + 1) rows × (2 × halfWidth) taps, rows normalized

    private var history: [Float]        // mono input; history[0] is the oldest sample still needed
    private var position: Double        // next output's read position in `history`
    private var realEnd: Int? = nil     // set by flush(): samples past this are padding

    public init(inputRate: Double, channels: Int, outputRate: Double) {
        precondition(inputRate > 0 && outputRate > 0 && channels > 0)
        self.inputRate = inputRate
        self.outputRate = outputRate
        self.channels = channels
        step = inputRate / outputRate
        // Cut off a little below the lower Nyquist so aliasing stays out of band.
        let cutoff = min(1.0, outputRate / inputRate) * 0.9
        let zeroCrossings = 10.0
        halfWidth = Int((zeroCrossings / cutoff).rounded(.up))
        let taps = 2 * halfWidth
        var t = [Float](repeating: 0, count: (Self.phases + 1) * taps)
        for p in 0...Self.phases {
            let frac = Double(p) / Double(Self.phases)
            var row = [Double](repeating: 0, count: taps)
            var sum = 0.0
            for j in 0..<taps {
                let d = Double(j - halfWidth + 1) - frac          // tap distance from the read position
                let x = cutoff * d
                let sinc = x == 0 ? 1.0 : sin(Double.pi * x) / (Double.pi * x)
                let w = abs(d) >= Double(halfWidth) ? 0 : 0.5 * (1 + cos(Double.pi * d / Double(halfWidth)))
                row[j] = sinc * w
                sum += row[j]
            }
            for j in 0..<taps { t[p * taps + j] = Float(row[j] / sum) }   // unity gain at DC
        }
        table = t
        // Pre-pad with silence so the first output lands on input sample 0.
        history = [Float](repeating: 0, count: halfWidth - 1)
        position = Double(halfWidth - 1)
    }

    /// Feeds interleaved samples (`channels` per frame); returns the mono
    /// output samples that are now complete.
    public func process(_ interleaved: [Float]) -> [Float] {
        let frames = interleaved.count / channels
        if channels == 1 {
            history.append(contentsOf: interleaved)
        } else {
            history.reserveCapacity(history.count + frames)
            let scale = 1 / Float(channels)
            interleaved.withUnsafeBufferPointer { p in
                for f in 0..<frames {
                    var s: Float = 0
                    for c in 0..<channels { s += p[f * channels + c] }
                    history.append(s * scale)
                }
            }
        }
        return drain()
    }

    /// Emits the remaining output up to the last input sample. The resampler
    /// is finished afterwards.
    public func flush() -> [Float] {
        guard realEnd == nil else { return [] }
        realEnd = history.count
        history.append(contentsOf: [Float](repeating: 0, count: halfWidth + 1))
        return drain()
    }

    private func drain() -> [Float] {
        let taps = 2 * halfWidth
        var out: [Float] = []
        history.withUnsafeBufferPointer { h in
            table.withUnsafeBufferPointer { tb in
                while true {
                    let center = Int(position)
                    if let end = realEnd, position >= Double(end) { break }
                    guard center + halfWidth < h.count else { break }
                    let phase = Int(((position - Double(center)) * Double(Self.phases)).rounded())
                    let row = phase * taps
                    let start = center - halfWidth + 1
                    var acc: Float = 0
                    for j in 0..<taps { acc += h[start + j] * tb[row + j] }
                    out.append(acc)
                    position += step
                }
            }
        }
        // Drop input no future output can reach.
        let drop = max(0, Int(position) - halfWidth + 1)
        if drop > 0 {
            history.removeFirst(min(drop, history.count))
            position -= Double(drop)
            if let end = realEnd { realEnd = end - drop }
        }
        return out
    }
}
