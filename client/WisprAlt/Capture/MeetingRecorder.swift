import AVFoundation
import CoreMedia
import ScreenCaptureKit
import AppKit

// MARK: - MeetingRecorderError

enum MeetingRecorderError: Error, LocalizedError {
    case noDisplayAvailable
    case alreadyRunning
    case notRunning

    var errorDescription: String? {
        switch self {
        case .noDisplayAvailable:
            return "No display found. Connect a display and try again."
        case .alreadyRunning:
            return "Meeting recording is already in progress."
        case .notRunning:
            return "Meeting recording is not running."
        }
    }
}

// MARK: - MeetingRecorder

/// Dual-channel meeting recorder using `SCStream`.
///
/// Channel mapping (matches server expectation):
/// - Channel 0 (mic): `SCStreamOutputType.microphone` (macOS 14+)
/// - Channel 1 (system): `SCStreamOutputType.audio`
///
/// Both SCStream output callbacks are dispatched on the single private serial queue
/// `ioQueue` (v3 P5#8), which eliminates concurrent writes to `stereoFile` and
/// removes the need for additional locking around `AVAudioFile.write()`.
///
/// ## Singleton
/// `MeetingRecorder.shared` is the single instance. `DictationRecorder` and
/// `MenuBarController` read `.isActive` from it.
final class MeetingRecorder: NSObject {

    // MARK: - Singleton

    static let shared = MeetingRecorder()

    // MARK: - Public state

    /// `true` while capture is active. Queried by `DictationRecorder.start()` for mic exclusion.
    private(set) var isActive: Bool = false

    // MARK: - Private capture state

    private var stream: SCStream?
    private var outputURL: URL?

    // MARK: - Per-channel converters (v3 P4#8: retained, not recreated per call)

    private var micConverter = CMSampleBufferConverter()
    private var sysConverter = CMSampleBufferConverter()

    // MARK: - Aligned ring buffer

    private var aligned = AlignedRingBuffer()

    // MARK: - AVAudioFile — written exclusively from ioQueue (v3 P5#8)

    private var stereoFile: AVAudioFile?

    // MARK: - Serial I/O queue
    //
    // Used as `sampleHandlerQueue` for BOTH `.audio` and `.microphone` outputs,
    // which serialises all SCStream callbacks onto a single thread. This is the
    // v3 P5#8 fix that eliminates the AVAudioFile concurrent-write race.

    private let ioQueue = DispatchQueue(
        label: "co.wispralt.meeting.io",
        qos: .userInteractive
    )

    // MARK: - startPTS locking
    //
    // Both .audio and .microphone callbacks race on `startPTS` assignment
    // even though they run on the same queue (the first callback wins; the
    // second sees a non-nil value). The lock is kept for correctness in case
    // queue semantics change, and has negligible overhead (always uncontended
    // on a serial queue).

    private var startPTS: CMTime?
    private var ptsLock = os_unfair_lock_s()

    // MARK: - Wall-clock flush timer (100 ms)

    private var flushTimer: DispatchSourceTimer?

    // MARK: - Sleep/wake observer

    private var sleepWakeObserver: NSObjectProtocol?

    // MARK: - Init

    private override init() {
        super.init()
    }

    // MARK: - Start

    /// Starts dual-channel meeting capture and writes output to `url`.
    ///
    /// - Parameter url: Destination WAV file URL (will be overwritten if it exists).
    /// - Throws: `MeetingRecorderError.alreadyRunning` if a session is in progress.
    ///   `MeetingRecorderError.noDisplayAvailable` if no display is enumerable
    ///   (v3 P5#13).
    func start(to url: URL) async throws {
        guard !isActive else { throw MeetingRecorderError.alreadyRunning }

        // Enumerate shareable content to get the primary display.
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: false
        )

        // v3 P5#13: guard on displays.first with actionable error.
        guard let primaryDisplay = content.displays.first else {
            throw MeetingRecorderError.noDisplayAvailable
        }

        // Reset per-session state.
        aligned = AlignedRingBuffer()
        os_unfair_lock_lock(&ptsLock)
        startPTS = nil
        os_unfair_lock_unlock(&ptsLock)
        micConverter = CMSampleBufferConverter()
        sysConverter = CMSampleBufferConverter()
        outputURL = url

        // Configure SCStream.
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        // Request 16 kHz from SCStream; the converter handles whatever rate is actually delivered.
        cfg.sampleRate = 48_000   // SCStream delivers 48kHz internally; we convert to 16k below.
        cfg.channelCount = 1

        if #available(macOS 14.0, *) {
            cfg.captureMicrophone = true
        }

        // Minimal video: 2×2 frame at 1 fps to satisfy SCStream's video requirement
        // while consuming effectively zero CPU.
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let filter = SCContentFilter(
            display: primaryDisplay,
            excludingApplications: [],
            exceptingWindows: []
        )

        stream = SCStream(filter: filter, configuration: cfg, delegate: self)

        // Both outputs on ioQueue (v3 P5#8 single-queue fix).
        try stream?.addStreamOutput(self, type: .audio, sampleHandlerQueue: ioQueue)

        if #available(macOS 14.0, *) {
            try stream?.addStreamOutput(self, type: .microphone, sampleHandlerQueue: ioQueue)
        }

        // Open the output AVAudioFile.
        stereoFile = try AVAudioFile(
            forWriting: url,
            settings: AudioFormat.canonical16kFloat32Stereo.settings,
            commonFormat: .pcmFormatFloat32,
            interleaved: false
        )

        try await stream?.startCapture()

        // Install 100 ms flush timer for wall-clock stall detection (v3 P5#8).
        startFlushTimer()

        // Register for sleep/wake notifications (v3 P5#5).
        sleepWakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.handleSystemWake()
        }

        isActive = true
        Log.info("MeetingRecorder: capture started → \(url.lastPathComponent)", category: "capture")
    }

    // MARK: - Stop

    /// Stops capture, flushes all buffered audio, and closes the output file.
    ///
    /// - Returns: The URL of the completed WAV file.
    /// - Throws: `MeetingRecorderError.notRunning` if no session is active.
    func stop() async throws -> URL {
        guard isActive, let url = outputURL else {
            throw MeetingRecorderError.notRunning
        }

        // Stop the wall-clock timer first.
        stopFlushTimer()

        // Remove sleep/wake observer.
        if let obs = sleepWakeObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(obs)
            sleepWakeObserver = nil
        }

        // Stop SCStream capture.
        try? await stream?.stopCapture()
        stream = nil

        // Final alignment: pad any lagging channel with silence up to the longest tail.
        ioQueue.sync {
            self.aligned.padMissing(toEnd: true)
            self.drainToFile(forceFlush: true)

            // v3 P4#14: nil out stereoFile inside the ioQueue block so the AVAudioFile
            // is deallocated (and its RIFF/WAVE header finalised) before we return.
            self.stereoFile = nil
        }

        isActive = false
        Log.info("MeetingRecorder: capture stopped → \(url.lastPathComponent)", category: "capture")
        return url
    }

    // MARK: - Private: flush aligned buffer to file

    /// Drains all available aligned frames from the ring buffer and writes them to
    /// `stereoFile`. Must be called on `ioQueue`.
    private func drainToFile(forceFlush: Bool = false) {
        while let frame = aligned.flushAligned(forceFlush: forceFlush) {
            do {
                try stereoFile?.write(from: frame)
            } catch {
                Log.error("MeetingRecorder: AVAudioFile write error: \(error)", category: "capture")
            }
        }
    }

    // MARK: - Private: flush timer

    private func startFlushTimer() {
        let timer = DispatchSource.makeTimerSource(queue: ioQueue)
        timer.schedule(deadline: .now() + 0.1, repeating: 0.1, leeway: .milliseconds(20))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let now = Date().timeIntervalSinceReferenceDate
            if self.aligned.forceFlushIfStalled(now: now) {
                self.drainToFile(forceFlush: true)
            } else {
                self.drainToFile(forceFlush: false)
            }
        }
        timer.resume()
        flushTimer = timer
    }

    private func stopFlushTimer() {
        flushTimer?.cancel()
        flushTimer = nil
    }

    // MARK: - Private: sleep/wake handler

    private func handleSystemWake() {
        ioQueue.async { [weak self] in
            guard let self, self.isActive else { return }
            Log.info("MeetingRecorder: system wake detected — force-flushing aligned buffer with silence pad.", category: "capture")
            self.aligned.padMissing(toEnd: false)
            self.drainToFile(forceFlush: true)
        }
    }
}

// MARK: - SCStreamOutput

extension MeetingRecorder: SCStreamOutput {

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        // All callbacks arrive on ioQueue (both types share the same queue — v3 P5#8).
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)

        // Determine and latch the recording start PTS (first arriving buffer wins).
        var localStart: CMTime
        os_unfair_lock_lock(&ptsLock)
        if startPTS == nil { startPTS = pts }
        localStart = startPTS!
        os_unfair_lock_unlock(&ptsLock)

        // Compute sample offset using CMTimeSubtract (not float subtraction) to avoid
        // accumulating drift over multi-hour recordings (R1#18 / known gotcha).
        let elapsed = CMTimeSubtract(pts, localStart)
        let offsetSamples = max(0, Int(CMTimeGetSeconds(elapsed) * 16_000.0))

        // Convert via the retained per-channel converter (v3 P4#8).
        switch outputType {
        case .audio:
            guard let buf = sysConverter.convertTo16kMono(sampleBuffer) else { return }
            aligned.append(buf, atSamplePos: offsetSamples, channel: .system)

        case .microphone:
            guard let buf = micConverter.convertTo16kMono(sampleBuffer) else { return }
            aligned.append(buf, atSamplePos: offsetSamples, channel: .mic)

        @unknown default:
            break
        }

        // Drain after every append; typically returns quickly when the peer channel
        // hasn't caught up yet.
        drainToFile(forceFlush: false)
    }
}

// MARK: - SCStreamDelegate

extension MeetingRecorder: SCStreamDelegate {

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        Log.error("MeetingRecorder: SCStream stopped with error: \(error)", category: "capture")
        // Record the stream stopping so the UI can surface it.
        // Full stop/cleanup is left to the caller via stop().
        isActive = false
    }
}
