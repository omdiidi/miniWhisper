import Foundation
import AVFoundation

/// Pre-upload helper that extracts the audio track from a video container into
/// a temp `.m4a` (or `.mp4` fallback). The original video is left untouched.
///
/// Callers should:
/// 1. Pass the user-picked URL to ``extractAudioIfVideo(_:)``.
/// 2. Upload the returned URL (which may equal the input if no extraction was
///    needed or if extraction failed — degrade gracefully).
/// 3. Delete the parent temp directory after the upload completes if the
///    returned URL is different from the input (the parent dir is unique per
///    call so a single `removeItem` cleans up everything).
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
    /// If *url* points at a container with BOTH a video and an audio track,
    /// extract just the audio to a temp file and return that path. Otherwise
    /// return *url* unchanged.
    ///
    /// Never throws — every failure mode degrades to "return original URL"
    /// so the caller can still attempt the upload and surface a meaningful
    /// server-side error if it really is unprocessable.
    static func extractAudioIfVideo(_ url: URL) async -> URL {
        let asset = AVURLAsset(url: url)

        // Probe tracks. If either probe throws, treat as "no extraction".
        let videoTracks: [AVAssetTrack]
        let audioTracks: [AVAssetTrack]
        do {
            videoTracks = try await asset.loadTracks(withMediaType: .video)
            audioTracks = try await asset.loadTracks(withMediaType: .audio)
        } catch {
            Log.warning(
                "AudioExtractor: track probe failed (\(error.localizedDescription)) — using original.",
                category: "transcribe"
            )
            return url
        }

        guard !videoTracks.isEmpty else {
            // Audio-only file (or unreadable) — no extraction needed.
            return url
        }
        guard !audioTracks.isEmpty else {
            // Video without audio — Whisper will surface the no-audio error.
            Log.info("AudioExtractor: video has no audio track, using original.", category: "transcribe")
            return url
        }

        // Unique temp directory so cleanup is a single `removeItem` on the
        // parent. Created up front, deleted on any failure path via `defer`.
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("wispralt-extract-\(UUID().uuidString)", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        } catch {
            Log.warning(
                "AudioExtractor: failed to create temp dir (\(error.localizedDescription)) — using original.",
                category: "transcribe"
            )
            return url
        }

        // R-L: only retain tempDir on success. If we fall through to "return url"
        // anywhere below, the deferred block nukes it.
        var keepTempDir = false
        defer {
            if !keepTempDir {
                try? FileManager.default.removeItem(at: tempDir)
            }
        }

        guard let export = AVAssetExportSession(
            asset: asset,
            presetName: AVAssetExportPresetPassthrough
        ) else {
            Log.warning("AudioExtractor: AVAssetExportSession init failed — using original.", category: "transcribe")
            return url
        }

        // R-E: `.m4a` is only valid for AAC-compatible audio. Probe support and
        // fall back to `.mp4` otherwise. Output extension matches.
        let outputType: AVFileType
        let outputExt: String
        if export.supportedFileTypes.contains(.m4a) {
            outputType = .m4a
            outputExt = "m4a"
        } else if export.supportedFileTypes.contains(.mp4) {
            outputType = .mp4
            outputExt = "mp4"
        } else {
            Log.warning(
                "AudioExtractor: neither .m4a nor .mp4 supported by passthrough export — using original.",
                category: "transcribe"
            )
            return url
        }

        let outURL = tempDir.appendingPathComponent("audio.\(outputExt)")
        export.outputFileType = outputType
        export.outputURL = outURL

        // R-D: completion-handler form wrapped in a checked continuation.
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            export.exportAsynchronously {
                cont.resume()
            }
        }

        guard export.status == .completed else {
            let err = export.error?.localizedDescription ?? "status=\(export.status.rawValue)"
            Log.warning("AudioExtractor: export failed (\(err)) — using original.", category: "transcribe")
            return url
        }

        // Sanity: output must be smaller than input to be worth keeping.
        // If the "audio-only" file ended up larger than the source, something
        // weird happened (e.g. all-video container with tiny audio re-muxed
        // into a larger m4a wrapper) — fall back to the original.
        let inSize = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        let outSize = (try? FileManager.default.attributesOfItem(atPath: outURL.path)[.size] as? Int) ?? 0
        guard outSize > 0, outSize < inSize else {
            Log.warning(
                "AudioExtractor: output (\(outSize)B) not smaller than input (\(inSize)B) — using original.",
                category: "transcribe"
            )
            return url
        }

        let inMB = Double(inSize) / 1_048_576.0
        let outMB = Double(outSize) / 1_048_576.0
        Log.info(
            String(format: "AudioExtractor: extracted audio %.1fMB → %.1fMB (%.0f%% reduction)",
                   inMB, outMB, (1.0 - outMB / inMB) * 100.0),
            category: "transcribe"
        )

        keepTempDir = true
        return outURL
    }
}
