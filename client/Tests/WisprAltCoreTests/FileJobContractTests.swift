import Foundation
import Testing
import WisprAltCore

@Suite struct FileJobContractTests {
    /// Must match the server runner's cancel reason byte for byte, or a user
    /// cancel surfaces as a failure toast.
    @Test func cancelledByUserReason_matchesServer() {
        #expect(FileJobContract.cancelledByUserReason == "cancelled by user")
    }

    // MARK: Local job outputs

    @Test func formatsToDownload_sortedOrAllFour() {
        #expect(FileJobContract.formatsToDownload(["txt", "json", "srt"]) == ["json", "srt", "txt"])
        #expect(FileJobContract.formatsToDownload([]) == ["json", "srt", "vtt", "txt"])
    }

    /// Restart recovery reads back exactly where the job's outputs belong.
    @Test func savedJobOutput_roundTripsAndRejectsJunk() throws {
        let out = SavedJobOutput(outputDirectory: URL(fileURLWithPath: "/tmp/Custom Transcriptions/talk__1"),
                                 stem: "talk")
        let back = try #require(SavedJobOutput.decode(try out.encoded()))
        #expect(back == out)
        #expect(back.directoryURL.appendingPathComponent(back.stem).appendingPathExtension("srt").path
                == "/tmp/Custom Transcriptions/talk__1/talk.srt")
        #expect(SavedJobOutput.decode(nil) == nil)
        #expect(SavedJobOutput.decode(Data("not json".utf8)) == nil)
        #expect(SavedJobOutput.decode(Data(#"{"outputDirectory":"","stem":"x"}"#.utf8)) == nil)
    }

    // MARK: Online JSON

    @Test func onlineJSON_shape() throws {
        let t = OnlineTranscript(model: "gpt-transcribe", source: "talk.m4a", createdAt: Date(timeIntervalSince1970: 0),
                                 durationSeconds: 1200,
                                 chunks: [.init(index: 0, start_s: 0, end_s: 598.4, text: " Hello there. "),
                                          .init(index: 1, start_s: 598.4, end_s: 1200, text: "General Kenobi.")])
        let obj = try #require(try JSONSerialization.jsonObject(with: t.jsonData()) as? [String: Any])
        #expect(Set(obj.keys) == ["version", "mode", "model", "source", "created_at", "duration_s", "text", "chunks"])
        #expect(obj["version"] as? Int == 1)
        #expect(obj["mode"] as? String == "online")
        #expect(obj["created_at"] as? String == "1970-01-01T00:00:00Z")
        #expect(obj["text"] as? String == "Hello there. General Kenobi.")
        let chunks = try #require(obj["chunks"] as? [[String: Any]])
        #expect(Set(chunks[0].keys) == ["index", "start_s", "end_s", "text"])
        #expect(chunks[1]["start_s"] as? Double == 598.4)
    }

    // MARK: commitOutputs

    private func tempDir() throws -> URL {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent("wispralt-commit-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    private func stage(_ root: URL) throws -> [URL] {
        let staging = root.appendingPathComponent("out")
        try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true)
        return try ["a.txt", "a.json"].map {
            let u = staging.appendingPathComponent($0)
            try Data($0.utf8).write(to: u)
            return u
        }
    }

    @Test func commitOutputs_success_movesBoth() throws {
        let root = try tempDir()
        defer { try? FileManager.default.removeItem(at: root) }
        let staged = try stage(root)
        let target = root.appendingPathComponent("job")
        let fm = FileManager.default
        let dir = try FileJobContract.commitOutputs(
            staged: staged,
            makeDirectory: { try fm.createDirectory(at: target, withIntermediateDirectories: false); return target },
            move: { try fm.moveItem(at: $0, to: $1) }, remove: { try? fm.removeItem(at: $0) })
        #expect(dir == target)
        #expect(Set(try fm.contentsOfDirectory(atPath: target.path)) == ["a.txt", "a.json"])
        #expect(try fm.contentsOfDirectory(atPath: root.appendingPathComponent("out").path).isEmpty)
    }

    struct MoveFailed: Error {}

    @Test func commitOutputs_secondMoveThrows_removesFolder_rethrows() throws {
        let root = try tempDir()
        defer { try? FileManager.default.removeItem(at: root) }
        let staged = try stage(root)
        let target = root.appendingPathComponent("job")
        let fm = FileManager.default
        var moves = 0
        #expect(throws: MoveFailed.self) {
            try FileJobContract.commitOutputs(
                staged: staged,
                makeDirectory: { try fm.createDirectory(at: target, withIntermediateDirectories: false); return target },
                move: { moves += 1; if moves == 2 { throw MoveFailed() }; try fm.moveItem(at: $0, to: $1) },
                remove: { try? fm.removeItem(at: $0) })
        }
        #expect(!fm.fileExists(atPath: target.path))
    }

    @Test func commitOutputs_makeDirectoryThrows_createsNothing() throws {
        var moved = false, removed = false
        #expect(throws: MoveFailed.self) {
            try FileJobContract.commitOutputs(staged: [URL(fileURLWithPath: "/nonexistent/a.txt")],
                                              makeDirectory: { throw MoveFailed() },
                                              move: { _, _ in moved = true }, remove: { _ in removed = true })
        }
        #expect(!moved)
        #expect(!removed)
    }
}

@Suite struct OnlineUploadPlanTests {
    private func plan(_ ext: String = "m4a", _ bytes: Int = 5_000_000, _ d: Double? = 300) -> OnlineUploadPlan {
        OnlineUploadPlan.decide(ext: ext, bytes: bytes, durationSeconds: d, capSeconds: 600)
    }

    @Test func table() {
        #expect(plan("m4a", 25_000_000, 300) == .direct(contentType: "audio/mp4"))
        #expect(plan("m4a", 5_000_000, 900) == .decode)
        #expect(plan("mp3", 5_000_000, 600) == .direct(contentType: "audio/mpeg"))
        #expect(plan("mp3", 5_000_000, 601) == .decode)
        #expect(plan("wav", 5_000_000, nil) == .decode)
        #expect(plan("wav", 25_000_001, 300) == .decode)
        for ext in ["mov", "caf", "aiff", "aac"] { #expect(plan(ext) == .decode) }
        #expect(plan("M4A") == .direct(contentType: "audio/mp4"))
        for ext in ["mp3", "mp4", "m4a", "wav", "webm", "ogg", "flac", "mpeg", "mpga"] {
            #expect(plan(ext) != .decode)
        }
    }
}

@Suite struct AudioExtractPolicyTests {
    private func d(strict: Bool, video: Bool = true, audio: Bool = true, ok: Bool = true,
                   out: Int = 10, inB: Int = 100) -> AudioExtractPolicy {
        AudioExtractPolicy.decide(strict: strict, hasVideo: video, hasAudio: audio, exportOK: ok,
                                  outBytes: out, inBytes: inB)
    }

    @Test func table() {
        for strict in [true, false] {
            #expect(d(strict: strict, video: false) == .useOriginal)          // audio-only
            #expect(d(strict: strict) == .useExtracted)                       // smaller
        }
        #expect(d(strict: true, audio: false) == .fail(.noAudioTrack))
        #expect(d(strict: false, audio: false) == .useOriginal)
        #expect(d(strict: true, ok: false) == .fail(.exportFailed))
        #expect(d(strict: false, ok: false) == .useOriginal)
        #expect(d(strict: true, out: 0) == .fail(.exportFailed))
        #expect(d(strict: false, out: 0) == .useOriginal)
        #expect(d(strict: true, out: 100) == .useExtracted)                   // not smaller, strict
        #expect(d(strict: false, out: 100) == .useOriginal)                   // not smaller, today
    }
}

@Suite struct ChunkPlannerTests {
    private func check(_ ranges: [Range<Double>], duration: Double, max: Double) {
        #expect(ranges.first?.lowerBound == 0)
        #expect(ranges.last?.upperBound == duration)
        for (i, r) in ranges.enumerated() {
            #expect(r.upperBound - r.lowerBound <= max + 1e-9)
            if i < ranges.count - 1 {
                #expect(r.upperBound - r.lowerBound >= max - 20 - 1e-9)
                #expect(ranges[i + 1].lowerBound == r.upperBound)
            }
        }
    }

    @Test func randomEnvelopes_holdInvariants_deterministic() {
        var seed: UInt64 = 42
        func next() -> Double {                          // small seeded LCG: fast and repeatable
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return Double(seed >> 11) / Double(1 << 53)
        }
        for _ in 0..<20 {
            let duration = (1 + next() * 3600 * 10).rounded() / 10
            let max = next() < 0.5 ? 420.0 : 600.0
            let rms = (0..<Int(duration * 10)).map { _ in Float(next()) }
            let a = ChunkPlanner.plan(duration: duration, maxChunk: max, rms: rms)
            check(a, duration: duration, max: max)
            #expect(a == ChunkPlanner.plan(duration: duration, maxChunk: max, rms: rms))
        }
    }

    @Test func shortFile_oneRange() {
        #expect(ChunkPlanner.plan(duration: 300, maxChunk: 600, rms: []) == [0..<300])
    }

    @Test func boundary_isQuietestWindowInSearchSpan() {
        var rms = [Float](repeating: 1, count: 15_000)   // 1500 s
        rms[5_900] = 0                                   // 590 s
        let r = ChunkPlanner.plan(duration: 1500, maxChunk: 600, rms: rms)
        #expect(r.first == 0..<590)
        check(r, duration: 1500, max: 600)
    }

    @Test func remainder_neverUnderOneSecond() {
        let flat = [Float](repeating: 1, count: 12_100)
        // 600.5 s: a boundary at 600 would leave 0.5 s, so it moves to 599.5.
        #expect(ChunkPlanner.plan(duration: 600.5, maxChunk: 600, rms: flat) == [0..<599.5, 599.5..<600.5])
        for d in Array(stride(from: 600.1, through: 601.5, by: 0.1)) + [1199.7, 1200.5, 1201] {
            let r = ChunkPlanner.plan(duration: d, maxChunk: 600, rms: flat)
            check(r, duration: d, max: 600)
            if r.count > 1, let last = r.last { #expect(last.upperBound - last.lowerBound >= 1 - 1e-9) }
        }
    }
}
