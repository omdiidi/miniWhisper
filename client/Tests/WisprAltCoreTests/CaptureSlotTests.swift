import Foundation
import os
import Testing
import WisprAltCore

private final class FakeSession: @unchecked Sendable {
    var abortCount = 0
    func abort() { abortCount += 1 }
}

/// Mirrors the recorder's `start()` order: guard first, a step that may throw,
/// and `begin` only at the end.
private struct FakeRecorder {
    struct SetupFailed: Error {}
    let slot = CaptureSlot<FakeSession>()
    func start(failSetup: Bool) throws -> Bool {
        guard !slot.isActive else { return false }
        if failSetup { throw SetupFailed() }
        _ = slot.begin(FakeSession())
        return true
    }
}

@Suite struct CaptureSlotTests {
    @Test func beginWhileActive_returnsFalse() {
        let slot = CaptureSlot<FakeSession>()
        #expect(slot.begin(nil))
        #expect(!slot.begin(FakeSession()))
        #expect(slot.isActive)
    }

    @Test func end_handsOffSession_neverAbortsIt() {
        let slot = CaptureSlot<FakeSession>()
        let a = FakeSession(), b = FakeSession()
        #expect(slot.begin(a))
        #expect(slot.end() === a)
        #expect(!slot.isActive)
        #expect(slot.begin(b))
        #expect(a.abortCount == 0)
        #expect(slot.end() === b)
    }

    @Test func endWithNoSession_returnsNil() {
        let slot = CaptureSlot<FakeSession>()
        #expect(slot.begin(nil))
        #expect(slot.end() == nil)
        #expect(slot.end() == nil)
    }

    @Test func throwingSetup_leavesSlotInactive_nextStartSucceeds() throws {
        let r = FakeRecorder()
        #expect(throws: FakeRecorder.SetupFailed.self) { try r.start(failSetup: true) }
        #expect(!r.slot.isActive)
        #expect(try r.start(failSetup: false))
        #expect(try !r.start(failSetup: false))
    }

    @Test func concurrentBeginEnd_neverTwoActive() async {
        let slot = CaptureSlot<FakeSession>()
        let holders = OSAllocatedUnfairLock(initialState: (now: 0, max: 0, wins: 0))
        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<2 {
                group.addTask {
                    for _ in 0..<1_000 {
                        guard slot.begin(FakeSession()) else { continue }
                        holders.withLock { $0.now += 1; $0.max = max($0.max, $0.now); $0.wins += 1 }
                        holders.withLock { $0.now -= 1 }
                        _ = slot.end()
                    }
                }
            }
        }
        let h = holders.withLock { $0 }
        #expect(h.max == 1)
        #expect(h.wins > 0)
        #expect(!slot.isActive)
    }
}
