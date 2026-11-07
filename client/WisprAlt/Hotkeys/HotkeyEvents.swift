import Foundation

/// Delegate that receives FN-key events. All methods are called on the main actor
/// so implementations may safely update UI state.
@MainActor protocol FNKeyEventsDelegate: AnyObject {
    /// Called when FN has been held for ≥ holdThreshold seconds. Begin dictation capture.
    func dictationStart()

    /// Called when FN is released after a confirmed hold. Stop dictation capture and submit.
    func dictationStop()
}
