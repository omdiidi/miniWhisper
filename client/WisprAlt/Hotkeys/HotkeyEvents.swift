import Foundation

/// Delegate that receives FN-key events. All methods are called on the main actor
/// so implementations may safely update UI state.
///
/// Implementors (typically MenuBarController) are responsible for enforcing
/// mic mutual-exclusion: `dictationStart()` should be a no-op when
/// `MeetingRecorder.shared.isActive` is true.
@MainActor protocol FNKeyEventsDelegate: AnyObject {
    /// Called when FN has been held for ≥ holdThreshold seconds. Begin dictation capture.
    func dictationStart()

    /// Called when FN is released after a confirmed hold. Stop dictation capture and submit.
    func dictationStop()

    /// Called on a triple-tap of FN within the triple-tap window. Toggle meeting recording.
    func toggleMeetingRecording()
}
