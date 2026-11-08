import AVFoundation
import Foundation
import WisprAltCore

/// User-facing failures of an Online file job that are not OpenAI errors.
enum OnlineFileError: LogSafeError, LocalizedError {
    case needsLocal, noAudioTrack, extractFailed

    var errorDescription: String? {
        switch self {
        case .needsLocal: return "This file type needs Local mode."
        case .noAudioTrack: return "This video has no usable audio track."
        case .extractFailed: return "Could not extract audio."
        }
    }

    var logDescription: String { "onlineFile(\(self))" }
    var outcomeKind: ErrorOutcome { .failed(.other) }
}

/// Online file transcription (OpenAI, TXT + JSON). Reads the picked file in
/// place (never copied); every temp file lives in one
/// `temporaryDirectory/wispralt-online-<id>/`, removed when the job ends and
/// swept at launch (a quit skips `defer`). Writes to the transcripts folder
/// only on success, all or nothing. Runs off the main actor; hops to it only
/// for progress and the final folder commit. Never contacts the Local host.
struct OnlineFileJob: Sendable {
    static let tempPrefix = "wispralt-online-"

    let id = UUID().uuidString.lowercased()
    let source: URL
    let model: OpenAIModel
    /// (1-based chunk index, total chunks), on the main actor.
    let progress: @Sendable @MainActor (Int, Int) -> Void

    /// Removes `wispralt-online-*` dirs a quit left behind. Call at launch,
    /// before any job can start.
    static func sweepLeftovers() {
        let tmp = FileManager.default.temporaryDirectory
        let names = (try? FileManager.default.contentsOfDirectory(atPath: tmp.path)) ?? []
        for name in names where name.hasPrefix(tempPrefix) {
            try? FileManager.default.removeItem(at: tmp.appendingPathComponent(name))
        }
    }

    /// Runs the job; returns the new transcripts folder.
    func run() async throws -> URL {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(Self.tempPrefix + id, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let provider = OpenAIFileProvider(model: model, keyProvider: { OpenAIKey.current() }, onBilled: SpendStore.record)
        let createdAt = Date()
        // Cancelling the task cancels the in-flight upload (the provider's
        // deadline wrapper handles task cancellation).
        let chunks = try await transcribe(dir: dir, provider: provider)

        let stem = source.deletingPathExtension().lastPathComponent
        let duration = chunks.last?.end_s
        let transcript = OnlineTranscript(model: model.id, source: source.lastPathComponent, createdAt: createdAt,
                                          durationSeconds: duration, chunks: chunks)
        let out = dir.appendingPathComponent("out", isDirectory: true)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let txt = out.appendingPathComponent("\(stem).txt"), json = out.appendingPathComponent("\(stem).json")
        try Data((transcript.text + "\n").utf8).write(to: txt)
        try transcript.jsonData().write(to: json)
        try Task.checkCancellation()
        return try await MainActor.run {
            let fm = FileManager.default
            return try FileJobContract.commitOutputs(
                staged: [txt, json],
                makeDirectory: { try CustomTranscriptionsStore.makeJobDirectory(forStem: stem) },
                move: { try fm.moveItem(at: $0, to: $1) },
                remove: { try? fm.removeItem(at: $0) })
        }
    }

    // MARK: - Steps

    private func transcribe(dir: URL, provider: OpenAIFileProvider) async throws -> [OnlineTranscript.Chunk] {
        let cap = model.dictationCapSeconds
        // 1. Direct: an accepted format, ≤ 25 MB and ≤ the cap → one POST as is.
        if let chunk = try await direct(source, provider: provider, cap: cap, filename: source.lastPathComponent) {
            return [chunk]
        }
        // 2. Video → strict extraction into the job dir, then try direct again.
        let audio: URL
        do {
            audio = try await AudioExtractor.extract(source, strict: true, tempDir: dir)
        } catch ExtractError.noAudioTrack {
            throw OnlineFileError.noAudioTrack
        } catch is ExtractError {
            throw OnlineFileError.extractFailed
        }
        if audio != source, let chunk = try await direct(audio, provider: provider, cap: cap,
                                                         filename: audio.lastPathComponent) {
            return [chunk]
        }
        // 3. Decode → split at quiet places → one 16 kHz WAV per chunk, each
        //    prompted with the previous chunk's last 200 characters.
        guard let file = try? AVAudioFile(forReading: audio) else { throw OnlineFileError.needsLocal }
        let duration = Double(file.length) / file.processingFormat.sampleRate
        let rms: [Float]
        do { rms = try Mono16kWAV.rmsProfile(from: audio, windowMs: 1000 / ChunkPlanner.windowsPerSecond) }
        catch { throw OnlineFileError.needsLocal }
        let ranges = ChunkPlanner.plan(duration: duration, maxChunk: Double(cap), rms: rms)
        var chunks: [OnlineTranscript.Chunk] = []
        for (i, range) in ranges.enumerated() {
            try Task.checkCancellation()
            await progress(i + 1, ranges.count)
            let wav = try Mono16kWAV.make(from: audio, range: range)
            let prompt = chunks.last.map { String($0.text.suffix(200)) }
            let r = try await provider.transcribe(audio: wav, filename: "audio.wav", contentType: "audio/wav",
                                                  prompt: prompt, durationSeconds: range.upperBound - range.lowerBound)
            chunks.append(.init(index: i, start_s: range.lowerBound, end_s: range.upperBound, text: r.text))
        }
        return chunks
    }

    /// One POST of `url` as is when `OnlineUploadPlan` says direct; nil otherwise.
    private func direct(_ url: URL, provider: OpenAIFileProvider, cap: Int,
                        filename: String) async throws -> OnlineTranscript.Chunk? {
        let bytes = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        let duration = await MenuBarController.probeAudioDuration(url)
        guard case .direct(let type) = OnlineUploadPlan.decide(ext: url.pathExtension, bytes: bytes,
                                                               durationSeconds: duration, capSeconds: cap),
              let duration else { return nil }
        await progress(1, 1)
        let data = try Data(contentsOf: url)
        let r = try await provider.transcribe(audio: data, filename: filename, contentType: type,
                                              prompt: nil, durationSeconds: duration)
        return .init(index: 0, start_s: 0, end_s: duration, text: r.text)
    }
}
