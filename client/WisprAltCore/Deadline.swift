import Foundation

/// Hands out exactly one result: the first `resume` wins, later ones are
/// ignored, and `value()` returns it whether it arrived before or after.
final class ResumeOnce<T: Sendable>: @unchecked Sendable {
    private enum State { case idle, waiting(CheckedContinuation<T, Error>), done(Result<T, Error>) }
    private let lock = NSLock()
    private var state = State.idle

    /// True only for the first result.
    @discardableResult func resume(_ r: Result<T, Error>) -> Bool {
        lock.lock()
        switch state {
        case .done: lock.unlock(); return false
        case .idle: state = .done(r); lock.unlock(); return true
        case .waiting(let c): state = .done(r); lock.unlock(); c.resume(with: r); return true
        }
    }

    func value() async throws -> T {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<T, Error>) in
            lock.lock()
            switch state {
            case .idle: state = .waiting(c); lock.unlock()
            case .done(let r): lock.unlock(); c.resume(with: r)
            case .waiting: lock.unlock(); preconditionFailure("value() awaited twice")
            }
        }
    }
}

/// Runs `operation` with a deadline that never waits on the work after it
/// fires: on timeout (or outer cancellation) the caller gets the error at once,
/// `onTimeout` runs exactly once to close whatever the work is blocked on, and
/// the work is cancelled. A TaskGroup cannot do this, because it awaits all
/// of its children.
public func withDeadline<T: Sendable>(seconds: Double, onTimeout: @escaping @Sendable () -> Void,
                                      _ operation: @escaping @Sendable () async throws -> T) async throws -> T {
    let once = ResumeOnce<T>()
    let work = Task {
        do { let value = try await operation(); once.resume(.success(value)) }
        catch { once.resume(.failure(error)) }
    }
    let timer = Task {
        do { try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) } catch { return }
        if once.resume(.failure(ProviderError.deadlineExceeded)) { onTimeout(); work.cancel() }
    }
    defer { timer.cancel() }
    return try await withTaskCancellationHandler {
        try await once.value()
    } onCancel: {
        if once.resume(.failure(CancellationError())) { onTimeout(); work.cancel() }
    }
}
