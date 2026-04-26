import CoreAudio
import Foundation

// MARK: - AudioDeviceListenerContext

/// Reference-typed closure container. Using a class (not struct) ensures the
/// closure's capture lifetime is unambiguous and the value semantics are not
/// accidentally copied across threads when we pass it through Unmanaged.
private final class AudioDeviceListenerContext {
    let onChange: () -> Void
    init(onChange: @escaping () -> Void) { self.onChange = onChange }
}

// MARK: - File-scope C callback

/// File-scope C function pointer — stable address, safely referenced by both
/// AudioObjectAddPropertyListener and AudioObjectRemovePropertyListener.
/// Cannot be stored on a Swift class instance because AudioObjectPropertyListenerProc
/// is a C function pointer type.
private let audioDeviceListenerCallback: AudioObjectPropertyListenerProc = { _, _, _, clientData in
    guard let clientData else { return noErr }
    // takeUnretainedValue() does NOT bump the retain count — if we capture `ctx`
    // into a DispatchQueue.main.async closure, the closure holds a Swift reference
    // to a heap object whose lifetime is the C-callback frame, not the dispatched
    // block. Under teardown (deinit running concurrently with an in-flight
    // callback), `ctx` could be freed before the main-queue closure executes.
    //
    // Codex review caught this. The fix: copy the closure VALUE out of the context
    // before dispatching. Swift closures are reference-typed and the captured
    // closure carries its own lifetime — the dispatched block then holds the
    // closure directly, not the surrounding context.
    let ctx = Unmanaged<AudioDeviceListenerContext>.fromOpaque(clientData).takeUnretainedValue()
    let onChange = ctx.onChange
    DispatchQueue.main.async { onChange() }
    return noErr
}

// MARK: - AudioDeviceListener

/// Wraps a CoreAudio HAL `AudioObjectAddPropertyListener` call that fires
/// whenever the default system input device changes. Used by `MeetingRecorder`
/// to detect mid-recording mic switches (SCStream emits no device-change callback).
///
/// Lifetime: install before `SCStream.startCapture()`; nil out (triggering deinit)
/// before the SCStream teardown in `stop()`.
final class AudioDeviceListener {

    // MARK: - Nested error

    enum Error: Swift.Error {
        case cannotAdd(OSStatus)
    }

    // MARK: - Private state

    /// Property address for kAudioHardwarePropertyDefaultInputDevice.
    /// Declared `let` (immutable after init); local `var` copies are made where
    /// an inout argument is required by the CoreAudio API.
    private let address: AudioObjectPropertyAddress = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope:    kAudioObjectPropertyScopeGlobal,
        mElement:  kAudioObjectPropertyElementMain
    )

    /// Strong reference to the context object so Swift's ARC doesn't release it
    /// while the C callback may still hold a raw pointer to it.
    private var context: AudioDeviceListenerContext?

    /// Raw pointer passed to the CoreAudio C callback. Set to nil on init
    /// failure (after rolling back the Unmanaged retain) so deinit can skip
    /// the redundant release on the failure path.
    private var contextPtr: UnsafeMutableRawPointer?

    /// True only after `AudioObjectAddPropertyListener` returned `noErr`.
    /// Guards the matching `AudioObjectRemovePropertyListener` call in deinit.
    private var registered: Bool = false

    // MARK: - Init / deinit

    /// Registers a CoreAudio HAL listener for default input device changes.
    ///
    /// - Parameter onChange: Called on the main queue whenever the default
    ///   audio input device changes.
    /// - Throws: `AudioDeviceListener.Error.cannotAdd` if the HAL call fails.
    init(onChange: @escaping () -> Void) throws {
        let ctx = AudioDeviceListenerContext(onChange: onChange)
        // Retain the context so the C callback holds a stable raw pointer to it.
        let ptr = Unmanaged.passRetained(ctx).toOpaque()
        self.context = ctx
        self.contextPtr = ptr

        var addr = address  // local mutable copy for inout argument
        let status = AudioObjectAddPropertyListener(
            AudioObjectID(kAudioObjectSystemObject),
            &addr,
            audioDeviceListenerCallback,
            ptr
        )
        guard status == noErr else {
            // Roll back the retain on failure so we don't leak; nil out contextPtr
            // so deinit knows the rollback already happened and skips release.
            Unmanaged<AudioDeviceListenerContext>.fromOpaque(ptr).release()
            self.contextPtr = nil
            self.context = nil
            throw Error.cannotAdd(status)
        }
        registered = true
    }

    deinit {
        // If init threw, contextPtr was nil-set after rolling back the retain.
        // Guard here prevents a double-release.
        guard let ptr = contextPtr else { return }

        // Only release the retain if AudioObjectRemovePropertyListener succeeds.
        // If Remove fails (e.g. kAudioHardwareBadObjectError during system shutdown
        // or HAL re-init), CoreAudio may still hold our raw pointer and could fire
        // the callback after we'd otherwise have freed the context — use-after-free.
        // Codex review caught this. On Remove failure we leak the context (a tiny
        // class instance) rather than risk UAF.
        var canRelease = true
        if registered {
            var addr = address  // local mutable copy for inout argument
            let status = AudioObjectRemovePropertyListener(
                AudioObjectID(kAudioObjectSystemObject),
                &addr,
                audioDeviceListenerCallback,
                ptr
            )
            if status != noErr {
                // Don't release — the callback may still fire. Tiny intentional leak.
                canRelease = false
            }
        }
        if canRelease {
            // Balance the Unmanaged.passRetained() from init exactly once.
            Unmanaged<AudioDeviceListenerContext>.fromOpaque(ptr).release()
        }
    }
}
