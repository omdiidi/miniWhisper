import Foundation

/// Splits long audio at quiet places for Online file jobs.
public enum ChunkPlanner {
    /// RMS windows per second the planner expects (`Mono16kWAV.rmsProfile(windowMs: 100)`).
    public static let windowsPerSecond = 10
    /// How far before the cap a boundary may move to find a quiet spot.
    public static let searchSeconds = 20

    /// Boundary *i* is the lowest-RMS 100 ms window in
    /// `[b(i−1) + maxChunk − 20, b(i−1) + maxChunk]` (ties → the later one),
    /// never within 1 s of the end. Every range is ≤ `maxChunk`; every range
    /// but the last is ≥ `maxChunk − 20`; the last is ≥ 1 s whenever there is
    /// more than one (a merge could never fit: previous + remainder > max).
    /// Deterministic for a given input.
    public static func plan(duration: Double, maxChunk: Double, rms: [Float]) -> [Range<Double>] {
        guard duration > 0, maxChunk > 0 else { return [] }
        // Work in whole windows so boundaries are exact tenths of a second.
        let wps = windowsPerSecond
        func t(_ w: Int) -> Double { Double(w) / Double(wps) }
        let maxW = Int((maxChunk * Double(wps)).rounded(.down))
        let searchW = searchSeconds * wps
        let lastW = Int((duration * Double(wps)).rounded(.down)) - wps   // 1 s before the end
        var ranges: [Range<Double>] = []
        var startW = 0
        while duration - t(startW) > maxChunk {
            let hi = min(startW + maxW, max(startW + 1, lastW))
            let lo = min(hi, max(startW + 1, startW + maxW - searchW))
            var best = hi
            var bestRMS = Float.infinity
            for w in lo...hi where w < rms.count && rms[w] <= bestRMS {
                best = w
                bestRMS = rms[w]
            }
            ranges.append(t(startW)..<t(best))
            startW = best
        }
        ranges.append(t(startW)..<duration)
        return ranges
    }
}
