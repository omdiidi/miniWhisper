import Foundation
import os
import Testing
@testable import WisprAltCore

/// Serialized: each test blocks a cooperative thread on purpose; in parallel with
/// itself on a small CI runner that starved the deadline timer.
@Suite(.serialized) struct DeadlineTests {
    private final class Counter: @unchecked Sendable {
        private let n = OSAllocatedUnfairLock(initialState: 0)
        func bump() { n.withLock { $0 += 1 } }
        var value: Int { n.withLock { $0 } }
    }

    @Test func workFinishesFirst_returnsValue_noTimeout() async throws {
        let fired = Counter()
        let v = try await withDeadline(seconds: 2, onTimeout: { fired.bump() }) { 42 }
        #expect(v == 42)
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(fired.value == 0)
    }

    @Test func workIgnoringCancellation_timesOutWithinBudget_onTimeoutOnce() async {
        let fired = Counter()
        let start = Date()
        do {
            _ = try await withDeadline(seconds: 0.2, onTimeout: { fired.bump() }) { () -> Int in
                Thread.sleep(forTimeInterval: 2.5)          // ignores cancellation
                return 1
            }
            Issue.record("expected deadlineExceeded")
        } catch {
            #expect(error as? ProviderError == .deadlineExceeded)
        }
        #expect(Date().timeIntervalSince(start) < 1.5)   // well short of the 2.5 s work; loose for busy CI hosts
        #expect(fired.value == 1)
    }

    @Test func outerCancel_throwsCancellation_onTimeoutOnce() async {
        let fired = Counter()
        let task = Task {
            try await withDeadline(seconds: 5, onTimeout: { fired.bump() }) { () -> Int in
                Thread.sleep(forTimeInterval: 1.0)
                return 1
            }
        }
        try? await Task.sleep(nanoseconds: 100_000_000)
        task.cancel()
        let result = await task.result
        #expect(throws: CancellationError.self) { try result.get() }
        #expect(fired.value == 1)
    }

    @Test func timeoutThenOuterCancel_onTimeoutStillOnce() async {
        let fired = Counter()
        let task = Task {
            try await withDeadline(seconds: 0.1, onTimeout: { fired.bump() }) { () -> Int in
                Thread.sleep(forTimeInterval: 4.0)   // long enough for the 0.1 s timer even on a busy runner
                return 1
            }
        }
        // Cancel only once the timeout has fired (a fixed sleep raced a busy test pool).
        for _ in 0..<1000 where fired.value == 0 { try? await Task.sleep(nanoseconds: 10_000_000) }
        task.cancel()
        let result = await task.result
        #expect(throws: ProviderError.deadlineExceeded) { try result.get() }
        #expect(fired.value == 1)
    }
}
