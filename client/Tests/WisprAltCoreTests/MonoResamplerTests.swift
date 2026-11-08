import Foundation
import Testing
import WisprAltCore

@Suite struct MonoResamplerTests {
    private static func stereoSine(hz: Double, rate: Double, seconds: Double, amp: Float = 0.5,
                                   invertRight: Bool = false) -> [Float] {
        let n = Int(rate * seconds)
        var out = [Float](repeating: 0, count: n * 2)
        for i in 0..<n {
            let s = amp * Float(sin(2 * Double.pi * hz * Double(i) / rate))
            out[2 * i] = s
            out[2 * i + 1] = invertRight ? -s : s
        }
        return out
    }

    private static func rms(_ x: ArraySlice<Float>) -> Double {
        (x.reduce(0.0) { $0 + Double($1) * Double($1) } / Double(x.count)).squareRoot()
    }

    /// Goertzel power at `hz` over the whole slice.
    private static func goertzelPower(_ x: ArraySlice<Float>, hz: Double, rate: Double) -> Double {
        let w = 2 * Double.pi * hz / rate
        let c = 2 * cos(w)
        var s1 = 0.0, s2 = 0.0
        for v in x { let s = Double(v) + c * s1 - s2; s2 = s1; s1 = s }
        return s1 * s1 + s2 * s2 - c * s1 * s2
    }

    private static func resampleAll(_ x: [Float], to rate: Double) -> [Float] {
        let r = MonoResampler(inputRate: 48_000, channels: 2, outputRate: rate)
        return r.process(x) + r.flush()
    }

    @Test(arguments: [16_000.0, 24_000.0])
    func sine1k_keepsLevelAndFrequency(rate: Double) {
        let out = Self.resampleAll(Self.stereoSine(hz: 1_000, rate: 48_000, seconds: 1), to: rate)
        #expect(abs(out.count - Int(rate)) <= 1)
        // Skip the filter's edges; use a whole number of 1 kHz cycles.
        let cycle = Int(rate / 1_000)
        let body = out[cycle * 10 ..< cycle * 10 + cycle * 900]
        let inRMS = 0.5 / 2.0.squareRoot()
        let dB = 20 * log10(Self.rms(body) / inRMS)
        #expect(abs(dB) <= 1)
        // Goertzel: ≥ 99 % of the energy sits at 1 kHz (no aliasing / distortion).
        let total = body.reduce(0.0) { $0 + Double($1) * Double($1) }
        let atTone = 2 * Self.goertzelPower(body, hz: 1_000, rate: rate) / Double(body.count)
        #expect(abs(atTone / total - 1) <= 0.01)
        #expect(out.allSatisfy { abs($0) <= 1 })
    }

    @Test func silence_staysSilence() {
        let out = Self.resampleAll([Float](repeating: 0, count: 48_000 * 2), to: 16_000)
        #expect(out.allSatisfy { $0 == 0 })
    }

    @Test func outOfPhaseChannels_averageToZero() {
        let out = Self.resampleAll(Self.stereoSine(hz: 1_000, rate: 48_000, seconds: 1, invertRight: true), to: 16_000)
        #expect(out.allSatisfy { abs($0) < 1e-6 })
    }

    @Test(arguments: [16_000.0, 24_000.0])
    func chunked_equalsOneShot(rate: Double) {
        let input = Self.stereoSine(hz: 440, rate: 48_000, seconds: 1.3)
        let oneShot = Self.resampleAll(input, to: rate)
        let r = MonoResampler(inputRate: 48_000, channels: 2, outputRate: rate)
        var chunked: [Float] = []
        var i = 0
        while i < input.count {
            let end = min(i + 4_096 * 2, input.count)
            chunked += r.process(Array(input[i..<end]))
            i = end
        }
        chunked += r.flush()
        #expect(abs(chunked.count - oneShot.count) <= 1)
        let n = min(chunked.count, oneShot.count)
        #expect((0..<n).allSatisfy { abs(chunked[$0] - oneShot[$0]) <= 1e-5 })
    }
}
