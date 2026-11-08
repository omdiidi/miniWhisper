import AVFoundation
import Foundation
import Testing
import WisprAltCore

/// Writes a native-rate Float32 recording like the dictation recorder does.
func writeTestRecording(seconds: Double, rate: Double = 48_000, channels: AVAudioChannelCount = 2,
                        hz: Double = 440) throws -> URL {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("wispralt-test-\(UUID().uuidString).wav")
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: rate, AVNumberOfChannelsKey: channels,
        AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: true,
    ]
    let file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
    let frames = AVAudioFrameCount(rate * seconds)
    let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: frames)!
    buffer.frameLength = frames
    for c in 0..<Int(channels) {
        for i in 0..<Int(frames) {
            buffer.floatChannelData![c][i] = 0.3 * Float(sin(2 * Double.pi * hz * Double(i) / rate))
        }
    }
    try file.write(from: buffer)
    return url
}

@Suite struct Mono16kWAVTests {
    private func u32(_ d: Data, _ at: Int) -> UInt32 { d.subdata(in: at..<at + 4).withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) } }
    private func u16(_ d: Data, _ at: Int) -> UInt16 { d.subdata(in: at..<at + 2).withUnsafeBytes { $0.loadUnaligned(as: UInt16.self) } }

    @Test func make_isRIFF16kMono16bit_about32000BytesPerSecond() throws {
        let url = try writeTestRecording(seconds: 2)
        defer { try? FileManager.default.removeItem(at: url) }
        let wav = try Mono16kWAV.make(from: url)
        #expect(String(data: wav.prefix(4), encoding: .ascii) == "RIFF")
        #expect(String(data: wav.subdata(in: 8..<12), encoding: .ascii) == "WAVE")
        #expect(u16(wav, 20) == 1)           // PCM
        #expect(u16(wav, 22) == 1)           // mono
        #expect(u32(wav, 24) == 16_000)
        #expect(u16(wav, 34) == 16)
        #expect(Int(u32(wav, 40)) == wav.count - 44)
        #expect(abs(wav.count - 44 - 64_000) <= 4)
    }

    @Test func make_range_readsOnlyThatPart() throws {
        let url = try writeTestRecording(seconds: 2)
        defer { try? FileManager.default.removeItem(at: url) }
        let wav = try Mono16kWAV.make(from: url, range: 0.5..<1.5)
        #expect(abs(wav.count - 44 - 32_000) <= 4)
    }

    @Test func rmsProfile_oneValuePerWindow() throws {
        let url = try writeTestRecording(seconds: 1)
        defer { try? FileManager.default.removeItem(at: url) }
        let profile = try Mono16kWAV.rmsProfile(from: url, windowMs: 100)
        #expect(profile.count == 10)
        #expect(profile.allSatisfy { abs($0 - Float(0.3 / 2.0.squareRoot())) < 0.01 })
    }

    @Test func unreadableFile_throws() {
        let url = URL(fileURLWithPath: "/nonexistent/\(UUID().uuidString).wav")
        #expect(throws: Mono16kWAV.Failure.unreadable) { try Mono16kWAV.make(from: url) }
    }
}
