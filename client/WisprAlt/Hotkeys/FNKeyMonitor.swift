import Cocoa
import CoreGraphics

// MARK: - Mach time helpers

/// Converts a Mach absolute time value to seconds using the host timebase info.
/// Cached on first call; safe to call from any thread.
private func machToSeconds(_ machTime: UInt64) -> Double {
    struct Cache {
        static let info: mach_timebase_info_data_t = {
            var info = mach_timebase_info_data_t()
            mach_timebase_info(&info)
            return info
        }()
    }
    return Double(machTime) * Double(Cache.info.numer) / Double(Cache.info.denom) * 1e-9
}

// MARK: - FNKeyMonitor

/// Monitors FN (Globe) key events via a `kCGSessionEventTap` (listen-only, no blocking).
///
/// Tap mask: `flagsChanged | keyDown`.
/// - Hold ≥ `holdThreshold` seconds → `dictationStart()` on FN-down confirmation, `dictationStop()` on FN-up.
/// - Triple-tap within `tripleWindow` seconds → `toggleMeetingRecording()`.
/// - FN + other key → ignored (pass-through for FN-arrow, FN-delete, etc.).
///
/// All state mutations happen on the private serial queue `q`. Delegate calls
/// hop to `.main` for `@MainActor` compatibility.
///
/// **macOS 14.4+ note**: `CGRequestListenEventAccess()` may return `true` but the actual
/// grant requires a process restart (TCC change takes effect on next launch). The caller
/// should detect this and present the "Quit and Reopen Required" sheet.
final class FNKeyMonitor {

    // MARK: - Public interface

    /// The delegate that receives high-level hotkey events.
    /// Must be set before calling `start(delegate:)`.
    private(set) weak var delegate: FNKeyEventsDelegate?

    // MARK: - Configuration
    //
    // holdThreshold and tripleWindow are read from Settings at use-site so that
    // changes in the Settings UI take effect immediately without restarting the
    // monitor.  Values are clamped to sane ranges on read.

    /// Seconds FN must be held before dictation is confirmed.
    /// Clamped to [0.10, 1.00] at read time; persisted clamping is in Settings.
    private var holdThreshold: Double {
        let v = Settings.shared.holdMinDuration
        return min(max(v, 0.10), 1.00)
    }

    /// Window in seconds within which three FN taps trigger meeting toggle.
    /// Clamped to [0.20, 0.80] at read time; persisted clamping is in Settings.
    private var tripleWindow: Double {
        let v = Settings.shared.tripleTapWindow
        return min(max(v, 0.20), 0.80)
    }

    // MARK: - Private state — ALL mutations on `q`

    /// Private serial queue for all state mutation. Named for debuggability.
    private let q = DispatchQueue(label: "co.wispralt.fn", qos: .userInteractive)

    /// Mach-time (seconds) of the current FN key-down event; nil when FN is up.
    private var fnDownTime: Double?

    /// Set to true when a non-FN key-down event is observed while FN is held.
    /// Prevents treat of FN modifier combos (FN+arrow, FN+delete, etc.) as taps/holds.
    private var otherKeyDuringFN: Bool = false

    /// Pending work item that fires after `holdThreshold` to confirm a hold.
    /// v3 P4#12: fires on `q`, NOT on `.main`.
    private var holdWorkItem: DispatchWorkItem?

    /// Records the DOWN timestamps for recent taps (R3#1: tap time = DOWN time, not UP).
    /// Cleared when a hold is confirmed to prevent stale taps poisoning future triple-taps.
    private var tapTimes: [Double] = []

    // MARK: - CGEventTap lifecycle

    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?

    // Retained self-pointer for the C callback. Released in `stop()`.
    private var selfPointer: Unmanaged<FNKeyMonitor>?

    // MARK: - Initialisation / teardown

    init() {}

    deinit {
        stop()
    }

    // MARK: - Start / stop

    /// Installs the CGEvent tap and begins monitoring FN key events.
    ///
    /// Returns `false` (logging a warning) if listen-event access is not granted.
    /// On macOS 14.4+, `CGRequestListenEventAccess()` returning `true` means the
    /// grant has been submitted to TCC but the process must be restarted before
    /// the tap will actually receive events.
    @discardableResult
    func start(delegate: FNKeyEventsDelegate) -> Bool {
        self.delegate = delegate

        guard checkOrRequestAccess() else {
            Log.error("FNKeyMonitor: listen-event access denied. Grant in System Settings → Privacy & Security → Input Monitoring.", category: "hotkeys")
            return false
        }

        let mask: CGEventMask = (1 << CGEventType.flagsChanged.rawValue) |
                                (1 << CGEventType.keyDown.rawValue)

        // Retain self for the C callback. Released in stop().
        let retained = Unmanaged.passRetained(self)
        selfPointer = retained

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .tailAppendEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: fnEventTapCallback,
            userInfo: retained.toOpaque()
        ) else {
            retained.release()
            selfPointer = nil
            Log.error("FNKeyMonitor: CGEvent.tapCreate failed. Check Input Monitoring permission.", category: "hotkeys")
            return false
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)

        Log.info("FNKeyMonitor: tap installed.", category: "hotkeys")
        return true
    }

    /// Removes the CGEvent tap and cleans up resources.
    func stop() {
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        eventTap = nil
        runLoopSource = nil
        selfPointer?.release()
        selfPointer = nil

        // Cancel any pending hold timer cleanly.
        q.async { [weak self] in
            self?.holdWorkItem?.cancel()
            self?.holdWorkItem = nil
            self?.fnDownTime = nil
            self?.tapTimes = []
        }

        Log.info("FNKeyMonitor: tap removed.", category: "hotkeys")
    }

    // MARK: - Event routing (called from C callback; hop to q immediately)

    /// Entry point from the CGEvent tap C callback.
    /// `t` is already converted to seconds via `machToSeconds`.
    fileprivate func handleEvent(type: CGEventType, flags: CGEventFlags) {
        // Capture timestamp as close to the event as possible.
        let t = machToSeconds(mach_absolute_time())
        let isFn = flags.contains(.maskSecondaryFn)

        q.async { [weak self] in
            guard let self else { return }
            switch type {
            case .flagsChanged:
                if isFn {
                    self.onFnDown(at: t)
                } else if self.fnDownTime != nil {
                    self.onFnUp(at: t)
                }
            case .keyDown:
                if self.fnDownTime != nil {
                    self.otherKeyDuringFN = true
                    self.holdWorkItem?.cancel()
                    self.holdWorkItem = nil
                }
            default:
                break
            }
        }
    }

    // MARK: - FN-key state machine (all run on `q`)

    private func onFnDown(at t: Double) {
        fnDownTime = t
        otherKeyDuringFN = false

        // v3 P4#12: hold timer fires on `q`, not on `.main`.
        // The work item hops back to `q` inside itself to safely read state
        // before dispatching the delegate call to `.main`.
        let item = DispatchWorkItem { [weak self] in
            guard let self,
                  self.fnDownTime != nil,
                  !self.otherKeyDuringFN else { return }

            // CRITICAL: clear tapTimes to prevent stale taps poisoning future triple-taps.
            self.tapTimes.removeAll()

            let del = self.delegate
            DispatchQueue.main.async {
                del?.dictationStart()
            }
        }
        holdWorkItem = item

        // Fire on the private queue after holdThreshold.
        q.asyncAfter(deadline: .now() + holdThreshold, execute: item)
    }

    private func onFnUp(at t: Double) {
        holdWorkItem?.cancel()
        holdWorkItem = nil

        guard let downTime = fnDownTime else { return }
        let duration = t - downTime
        fnDownTime = nil

        // FN + another key: ignore entirely — these are modifier combos (FN+arrow etc.).
        // Invariant: tapTimes must not contain spurious modifier-combo presses, so clear it.
        if otherKeyDuringFN {
            tapTimes.removeAll()
            return
        }

        if duration >= holdThreshold {
            // Confirmed hold release → stop dictation.
            // Clear tapTimes so stale short-press records from before the hold
            // cannot poison a subsequent triple-tap window.
            tapTimes.removeAll()
            let del = delegate
            DispatchQueue.main.async {
                del?.dictationStop()
            }
        } else {
            // Short tap: record the DOWN time (not UP) per R3#1.
            recordTap(at: downTime)
        }
    }

    private func recordTap(at downTime: Double) {
        tapTimes.append(downTime)

        // Keep only taps within the triple-tap window.
        let cutoff = downTime - tripleWindow
        tapTimes = tapTimes.filter { $0 > cutoff }

        if tapTimes.count >= 3 {
            tapTimes.removeAll()
            let del = delegate
            DispatchQueue.main.async {
                del?.toggleMeetingRecording()
            }
        }
    }

    // MARK: - Permission helpers

    /// Returns `true` if listen-event access is already granted.
    /// If not, triggers the TCC request dialog and returns `false`; the caller
    /// must re-check after the user acts (typically after a process restart on 14.4+).
    private func checkOrRequestAccess() -> Bool {
        if CGPreflightListenEventAccess() {
            return true
        }
        // Trigger the system permission dialog. On macOS 14.4+ this returns immediately
        // after requesting; the actual grant requires a process restart.
        CGRequestListenEventAccess()
        return CGPreflightListenEventAccess()
    }
}

// MARK: - C-compatible CGEvent tap callback

/// Free function usable as a CGEvent tap callback.
/// `userInfo` carries an `Unmanaged<FNKeyMonitor>` retained reference.
private func fnEventTapCallback(
    proxy: CGEventTapProxy,
    type: CGEventType,
    event: CGEvent,
    userInfo: UnsafeMutableRawPointer?
) -> Unmanaged<CGEvent>? {
    guard let ptr = userInfo else { return Unmanaged.passUnretained(event) }
    let monitor = Unmanaged<FNKeyMonitor>.fromOpaque(ptr).takeUnretainedValue()
    monitor.handleEvent(type: type, flags: event.flags)
    return Unmanaged.passUnretained(event)
}
