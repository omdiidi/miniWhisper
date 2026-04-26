import AVFoundation
import CoreMedia
import ScreenCaptureKit
import AppKit
import os
import os.lock

extension Notification.Name {
    /// Posted when the default audio input device changes mid-meeting recording.
    /// MenuBarController observes this to abort the in-flight meeting and
    /// return the UI to idle — SCStream emits no equivalent device-change callback,
    /// so we use the CoreAudio HAL listener in AudioDeviceListener instead.
    static let meetingConfigChanged = Notification.Name("co.wispralt.meetingConfigChanged")
}

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
    //
    // I1: `isActive` is read from @MainActor callers (MenuBarController, FNKeyMonitor)
    // AND written from `ioQueue` (SCStreamDelegate). Using OSAllocatedUnfairLock makes
    // the getter/setter data-race-free on macOS 13+ without requiring the caller to
    // hop queues.

    private let _isActiveLock = OSAllocatedUnfairLock(initialState: false)

    /// `true` while capture is active. Thread-safe; safe to read from any thread.
    var isActive: Bool {
        _isActiveLock.withLock { $0 }
    }

    /// Thread-safe setter, used internally instead of `isActive = newValue` to
    /// avoid Swift accessor-syntax issues with `private set` on locked values.
    private func setActive(_ value: Bool) {
        _isActiveLock.withLock { $0 = value }
    }

    // MARK: - Private capture state

    private var stream: SCStream?
    private var outputURL: URL?

    /// Snapshot of the most recently configured output URL. Survives stop() and
    /// SCStream auto-teardown so the config-change abort handler can delete a
    /// partial WAV even if isActive has already flipped to false. Cleared only
    /// at the start of the next start().
    private(set) var lastOutputURL: URL?

    /// CoreAudio HAL listener for default input device changes.
    /// Installed BEFORE stream.startCapture() and torn down as the first step
    /// of stop() and deinit so partial-init failures can't leave an orphan.
    private var deviceListener: AudioDeviceListener?

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

    private let ptsState = OSAllocatedUnfairLock<CMTime?>(initialState: nil)

    // MARK: - Wall-clock flush timer (100 ms)

    private var flushTimer: DispatchSourceTimer?

    // MARK: - Sleep/wake observers

    private var sleepWakeObserver: NSObjectProtocol?
    private var willSleepObserver: NSObjectProtocol?

    // MARK: - Max duration timers (C13)

    private var capTimer: DispatchSourceTimer?
    private var warningTimer: DispatchSourceTimer?

    // MARK: - Init

    private override init() {
        super.init()
    }

    // MARK: - Start

    /// Starts dual-channel meeting capture and writes output to `url`.
    ///
    /// - Parameters:
    ///   - url: Destination WAV file URL (will be overwritten if it exists).
    ///   - maxDuration: Hard recording cap in seconds (default 5400 = 90 min).
    ///     At 3600 s (60 min) a `.meetingApproachingCap` notification is posted.
    ///     At `maxDuration` a `.meetingMaxDurationReached` notification is posted.
    /// - Throws: `MeetingRecorderError.alreadyRunning` if a session is in progress.
    ///   `MeetingRecorderError.noDisplayAvailable` if no display is enumerable
    ///   (v3 P5#13).
    func start(to url: URL, maxDuration: TimeInterval = -1) async throws {
        // Resolve maxDuration: negative sentinel means "read from Settings at call time".
        let resolvedMaxDuration = maxDuration < 0
            ? TimeInterval(Settings.shared.maxMeetingMinutes * 60)
            : maxDuration
        let maxDuration = resolvedMaxDuration
        guard !isActive else { throw MeetingRecorderError.alreadyRunning }

        // Clear the previous session's URL snapshot before doing anything else.
        lastOutputURL = nil

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
        ptsState.withLock { $0 = nil }
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

        // Snapshot the output URL as soon as we know the path so the abort
        // handler can clean up a partial WAV even if SCStream's didStopWithError
        // beats the device-change notification and isActive is already false.
        lastOutputURL = url

        // Install the CoreAudio HAL device-change listener BEFORE starting
        // capture. Installing first means a partial-init failure (where
        // startCapture() throws) can't leave an orphan SCStream attached to
        // a listener that still fires. If listener init throws, we propagate
        // and let the caller see the failure; the stream was never started.
        deviceListener = try AudioDeviceListener { [weak self] in
            guard let self, self.isActive else { return }
            Log.info("Meeting: input device changed, aborting", category: "capture")
            NotificationCenter.default.post(name: .meetingConfigChanged, object: nil)
        }

        try await stream?.startCapture()

        // Install 100 ms flush timer for wall-clock stall detection (v3 P5#8).
        startFlushTimer()

        // C13: Install max-duration and approach-warning timers.
        startCapTimers(maxDuration: maxDuration)

        // Register for sleep/wake notifications (v3 P5#5).
        // willSleepObserver captures the Mach time just before sleep so the wake
        // handler can compute the exact gap duration for silence padding.
        willSleepObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.willSleepNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.preSleepMachTime = mach_absolute_time()
        }
        sleepWakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.handleSystemWake()
        }

        setActive(true)
        Log.info("MeetingRecorder: capture started → \(url.lastPathComponent)", category: "capture")
    }

    // MARK: - Cap timer helpers (C13)

    private func startCapTimers(maxDuration: TimeInterval) {
        // 60-minute warning.
        if maxDuration > 3600 {
            let warnTimer = DispatchSource.makeTimerSource(queue: .main)
            warnTimer.schedule(deadline: .now() + 3600, repeating: .never)
            warnTimer.setEventHandler {
                NotificationCenter.default.post(name: .meetingApproachingCap, object: nil)
            }
            warnTimer.resume()
            warningTimer = warnTimer
        }

        // Hard cap at maxDuration.
        let hardTimer = DispatchSource.makeTimerSource(queue: .main)
        hardTimer.schedule(deadline: .now() + maxDuration, repeating: .never)
        hardTimer.setEventHandler {
            NotificationCenter.default.post(name: .meetingMaxDurationReached, object: nil)
        }
        hardTimer.resume()
        capTimer = hardTimer
    }

    private func stopCapTimers() {
        capTimer?.cancel()
        capTimer = nil
        warningTimer?.cancel()
        warningTimer = nil
    }

    // MARK: - Stop

    /// Stops capture, flushes all buffered audio, and closes the output file.
    ///
    /// - Returns: The URL of the completed WAV file.
    /// - Throws: `MeetingRecorderError.notRunning` if no session is active.
    func stop() async throws -> URL {
        // Tear down all session resources unconditionally. Codex review caught
        // a leak: when SCStream's didStopWithError flips isActive=false BEFORE
        // stop() is called, the prior `guard isActive ... else { throw }` returned
        // early and skipped this teardown — leaving timers, sleep/wake observers,
        // stream, deviceListener, and stereoFile alive for the next session.
        // Now we always tear down, then throw `.notRunning` only AFTER cleanup
        // if there was nothing to return.

        // Tear down the device-change listener first so it can't fire during
        // the rest of the teardown sequence and post a redundant notification.
        deviceListener = nil

        // Stop the wall-clock timer and cap timers first.
        stopFlushTimer()
        stopCapTimers()

        // Remove sleep/wake observers.
        if let obs = willSleepObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(obs)
            willSleepObserver = nil
        }
        if let obs = sleepWakeObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(obs)
            sleepWakeObserver = nil
        }

        // Stop SCStream capture (idempotent — try? swallows "already stopped").
        try? await stream?.stopCapture()
        stream = nil

        // Final alignment: pad any lagging channel with silence up to the longest tail.
        // Drain may be a no-op if stereoFile was already nilled by a prior path.
        ioQueue.sync {
            self.aligned.padMissing(toEnd: true)
            self.drainToFile(forceFlush: true)
            self.stereoFile = nil
        }

        // Resolve the final URL state. If the session was never started or already
        // torn down by didStopWithError, throw .notRunning AFTER cleanup.
        let wasActive = isActive
        setActive(false)
        guard wasActive, let url = outputURL else {
            throw MeetingRecorderError.notRunning
        }
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

    // MARK: - Sleep/wake wall-clock tracking

    /// Mach absolute time captured just before the system goes to sleep.
    /// Set by a willSleepNotification observer; read by handleSystemWake.
    private var preSleepMachTime: UInt64 = 0

    // MARK: - Private: sleep/wake handler

    /// Called on wake.  Computes the wall-clock sleep gap, converts to samples,
    /// pads silence into the lagging channel, then drains the aligned buffer.
    ///
    /// ## Contract
    /// The sleep gap is converted to 16 kHz samples and added to the current
    /// maximum channel tail.  `padMissing(uptoSampleEnd:)` fills both channels
    /// up to that position.  Subsequent `flushAligned(forceFlush: true)` calls
    /// drain the padded data in normal-sized chunks.
    private func handleSystemWake() {
        let wakeTime = mach_absolute_time()
        ioQueue.async { [weak self] in
            guard let self, self.isActive else { return }
            Log.info("MeetingRecorder: system wake detected — inserting silence for sleep gap.", category: "capture")

            // Compute gap in seconds using Mach timebase (avoids float drift).
            let sleepMachDelta = preSleepMachTime > 0 ? wakeTime - preSleepMachTime : 0
            var timebase = mach_timebase_info_data_t()
            mach_timebase_info(&timebase)
            let gapSeconds = Double(sleepMachDelta) * Double(timebase.numer) / Double(timebase.denom) * 1e-9

            // Convert gap to sample count and compute the target end position.
            let gapSamples = Int(gapSeconds * 16_000.0)
            let micTail = self.aligned.channelTail(.mic)
            let sysTail = self.aligned.channelTail(.system)
            let currentMax = max(micTail, sysTail)
            let expectedEnd = currentMax + max(0, gapSamples)

            self.aligned.padMissing(uptoSampleEnd: expectedEnd)
            self.drainToFile(forceFlush: true)
        }
    }

    // MARK: - Deinit

    deinit {
        // Belt-and-suspenders: release the device listener if the recorder is
        // deallocated while recording is still active (app teardown, etc.).
        deviceListener = nil
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
        let localStart: CMTime = ptsState.withLock { current in
            if current == nil { current = pts }
            return current!
        }

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
        setActive(false)
    }
}
