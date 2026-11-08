import Foundation
import os

/// The recorder's single "a capture is running" flag plus the session it
/// owns. `begin` fails while a capture is active, and `end` hands the session
/// to the caller (it never aborts it), so a new capture can start the moment
/// the previous one has copied out its values, without touching its session.
public final class CaptureSlot<Session: AnyObject & Sendable>: @unchecked Sendable {
    private let state = OSAllocatedUnfairLock<(active: Bool, session: Session?)>(initialState: (false, nil))

    public init() {}

    public var isActive: Bool { state.withLock { $0.active } }

    public func begin(_ session: Session?) -> Bool {
        state.withLock { s in
            guard !s.active else { return false }
            s = (true, session)
            return true
        }
    }

    /// Ends the capture and returns its session.
    public func end() -> Session? {
        state.withLock { s in
            let out = s.session
            s = (false, nil)
            return out
        }
    }
}
