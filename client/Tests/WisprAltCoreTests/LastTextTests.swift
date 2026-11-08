import AVFoundation
import Foundation
import Testing
import WisprAltCore

@Suite struct LastTextTests {
    private func tempURL() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("wispralt-lt-\(UUID().uuidString)/last.json")
    }

    @Test func roundTrip_and0600() throws {
        let url = tempURL()
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        let t = LastText(text: "hello there", at: Date(timeIntervalSince1970: 1_800_000_000), mode: .online, modelUsed: "gpt-transcribe")
        try t.save(to: url)
        try t.save(to: url)   // overwrite stays 0600
        #expect(LastText.load(from: url, now: t.at.addingTimeInterval(60)) == t)
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        #expect((attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        let dirAttrs = try FileManager.default.attributesOfItem(atPath: url.deletingLastPathComponent().path)
        #expect((dirAttrs[.posixPermissions] as? NSNumber)?.intValue == 0o700)
    }

    @Test func expiresAfter7Days_andIsDeleted() throws {
        let url = tempURL()
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        let t = LastText(text: "x", at: Date(timeIntervalSince1970: 1_800_000_000), mode: .local, modelUsed: "local")
        try t.save(to: url)
        #expect(LastText.load(from: url, now: t.at.addingTimeInterval(LastText.maxAge)) == t)
        #expect(LastText.load(from: url, now: t.at.addingTimeInterval(LastText.maxAge + 1)) == nil)
        #expect(!FileManager.default.fileExists(atPath: url.path))
    }

    /// The launch rule depends on this: a spool CAF still open for writing
    /// (as after a crash) is readable with duration > 0.
    @Test func openSpoolCAF_isReadable() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("wispralt-caf-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("spool.caf"), copy = dir.appendingPathComponent("copy.caf")
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000.0, AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: true,
        ]
        let file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
        let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4_800)!
        buffer.frameLength = 4_800
        for i in 0..<4_800 { buffer.floatChannelData![0][i] = 0.2 * Float(sin(Double(i) / 10)) }
        for _ in 0..<20 { try file.write(from: buffer) }   // 2 s
        try withExtendedLifetime(file) {
            try FileManager.default.copyItem(at: url, to: copy)   // copied while still open
        }
        let read = try AVAudioFile(forReading: copy)
        #expect(Double(read.length) / read.processingFormat.sampleRate > 0)
        #expect(try Mono16kWAV.make(from: copy).count > 44)
    }
}
