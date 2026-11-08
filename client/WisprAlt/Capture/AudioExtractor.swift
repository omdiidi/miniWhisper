import Foundation
import AVFoundation
import WisprAltCore

/// Pre-upload helper that extracts the audio track from a video container into
/// a temp `.m4a` (or `.mp4` fallback). The original video is left untouched.
///
/// Callers:
/// - Local: ``extractAudioIfVideo(_:)`` (non-strict, never throws). Upload the
///   returned URL; if it differs from the input, delete its parent temp dir
///   afterwards (unique per call, so one `removeItem` cleans up).
/// - Online: ``extract(_:strict:tempDir:)`` with `strict: true` and the job's
///   own temp dir (the job removes that dir). Throws `ExtractError`.
///
/// Design notes:
/// - Uses `AVAssetExportPresetPassthrough` to copy the audio track without
///   re-encoding (fast, lossless).
/// - Uses the completion-handler form `exportAsynchronously(completionHandler:)`
///   wrapped in `withCheckedContinuation` — the no-arg `async` overload is
///   macOS 15+ only and even when available the completion form lets us
///   inspect `export.status` after completion uniformly.
/// - Validates `export.supportedFileTypes.contains(.m4a)` before assigning
///   `outputFileType = .m4a`; falls back to `.mp4` if the source's audio
///   codec isn't AAC-compatible.
enum AudioExtractor {
    /// Local path: never throws — every failure degrades to "return original
    /// URL" so the server can still try the file and report a real error.
    static func extractAudioIfVideo(_ url: URL) async -> URL {
        (try? await extract(url, strict: false, tempDir: nil)) ?? url
    }

    /// If *url* has BOTH a video and an audio track, extract just the audio
    /// and return its path; otherwise return *url* (decisions:
    /// `AudioExtractPolicy`). `strict` (Online) throws `ExtractError` instead of
    /// falling back, and keeps an extraction even when it is not smaller.
    /// With a `tempDir` (the Online job's own dir) the output goes there and
    /// the extractor never deletes it; without one it makes its own
    /// `wispralt-extract-<uuid>` dir, removed unless the extraction is used.
    static func extract(_ url: URL, strict: Bool, tempDir givenDir: URL?) async throws -> URL {
        let asset = AVURLAsset(url: url)
        let inSize = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        /// True → use the extraction; false → the original; throws in strict mode.
        /// Defaults describe "so far so good" (an extraction smaller than the source).
        func decide(video: Bool = true, audio: Bool = true, ok: Bool = true,
                    out: Int = 1, in inBytes: Int = 2) throws -> Bool {
            switch AudioExtractPolicy.decide(strict: strict, hasVideo: video, hasAudio: audio, exportOK: ok,
                                             outBytes: out, inBytes: inBytes) {
            case .useOriginal: return false
            case .useExtracted: return true
            case .fail(let e): throw e
            }
        }

        // Probe tracks. A probe failure returns the original in both modes;
        // the Online decode step then reports it.
        let videoTracks: [AVAssetTrack]
        let audioTracks: [AVAssetTrack]
        do {
            videoTracks = try await asset.loadTracks(withMediaType: .video)
            audioTracks = try await asset.loadTracks(withMediaType: .audio)
        } catch {
            Log.warning(
                "AudioExtractor: track probe failed (\(ErrorLog.describe(error))) — using original.",
                category: "transcribe"
            )
            return url
        }
        guard try decide(video: !videoTracks.isEmpty, audio: !audioTracks.isEmpty) else {
            Log.info("AudioExtractor: no extraction (audio-only, or video without audio).", category: "transcribe")
            return url
        }

        // Own temp dir unless the caller gave one; deleted on every path that
        // does not use the extraction (never the caller's dir).
        let tempDir = givenDir ?? FileManager.default.temporaryDirectory
            .appendingPathComponent("wispralt-extract-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        var keepTempDir = givenDir != nil
        defer {
            if !keepTempDir {
                try? FileManager.default.removeItem(at: tempDir)
            }
        }

        // R-E: `.m4a` is only valid for AAC-compatible audio. Probe support and
        // fall back to `.mp4` otherwise. Output extension matches.
        let export = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetPassthrough)
        let types = export?.supportedFileTypes ?? []
        let output: (type: AVFileType, ext: String)? =
            types.contains(.m4a) ? (.m4a, "m4a") : types.contains(.mp4) ? (.mp4, "mp4") : nil
        guard let export, let output else {
            Log.warning("AudioExtractor: passthrough export unavailable.", category: "transcribe")
            _ = try decide(ok: false)
            return url
        }

        let outURL = tempDir.appendingPathComponent("audio.\(output.ext)")
        export.outputFileType = output.type
        export.outputURL = outURL

        // R-D: completion-handler form wrapped in a checked continuation.
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            export.exportAsynchronously {
                cont.resume()
            }
        }

        guard export.status == .completed else {
            let cause = export.error.map { ErrorLog.describe($0) } ?? "none"
            Log.warning(
                "AudioExtractor: export failed (status=\(export.status.rawValue), \(cause)).",
                category: "transcribe"
            )
            _ = try decide(ok: false)
            return url
        }

        // Not smaller than the source (e.g. a tiny audio track re-muxed into a
        // larger wrapper): Local keeps the original; Online keeps the
        // extraction, since the original video may not decode.
        let outSize = (try? FileManager.default.attributesOfItem(atPath: outURL.path)[.size] as? Int) ?? 0
        guard try decide(out: outSize, in: inSize) else {
            Log.warning(
                "AudioExtractor: output (\(outSize)B) not smaller than input (\(inSize)B) — using original.",
                category: "transcribe"
            )
            return url
        }

        if inSize > 0 {
            Log.info(
                String(format: "AudioExtractor: extracted audio (%.0f%% of the source size)",
                       Double(outSize) / Double(inSize) * 100.0),
                category: "transcribe"
            )
        }
        keepTempDir = true
        return outURL
    }
}
