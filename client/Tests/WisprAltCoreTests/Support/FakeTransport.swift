import Foundation
import WisprAltCore

/// Scripted `WebSocketTransport` for `OpenAIRealtimeProvider` tests.
/// - `openFails`: the first send (the socket opening) throws `.notConnected`.
/// - `gated`: the first send waits until `openGate()`.
/// - `ignoresCancel`: `receive` never returns, even after `close`.
/// - `reply`: server events queued in answer to each sent message.
final class FakeTransport: WebSocketTransport, @unchecked Sendable {
    private let lock = NSLock()
    private let openFails: Bool
    private let ignoresCancel: Bool
    private let reply: @Sendable (String) -> [String]
    private var gate: CheckedContinuation<Void, Never>?
    private var gateOpen: Bool
    private var sentMessages: [String] = []
    private var inbox: [String] = []
    private var waiter: CheckedContinuation<String, Error>?
    private var hung: [CheckedContinuation<String, Error>] = []
    private var closed = false
    private var closes = 0

    init(openFails: Bool = false, gated: Bool = false, ignoresCancel: Bool = false,
         reply: @escaping @Sendable (String) -> [String] = { _ in [] }) {
        self.openFails = openFails
        self.gateOpen = !gated
        self.ignoresCancel = ignoresCancel
        self.reply = reply
    }

    var sent: [String] { lock.withLock { sentMessages } }
    var closeCount: Int { lock.withLock { closes } }

    func openGate() {
        let g: CheckedContinuation<Void, Never>? = lock.withLock { gateOpen = true; let g = gate; gate = nil; return g }
        g?.resume()
    }

    /// Pushes a server event as if the server sent it.
    func deliver(_ text: String) {
        let w: CheckedContinuation<String, Error>? = lock.withLock {
            if let w = waiter { waiter = nil; return w }
            inbox.append(text)
            return nil
        }
        w?.resume(returning: text)
    }

    func send(_ text: String) async throws {
        let first = lock.withLock { sentMessages.isEmpty && !closed }
        if first && openFails { throw ProviderError.notConnected }
        if first {
            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                let open: Bool = lock.withLock { if gateOpen { return true }; gate = c; return false }
                if open { c.resume() }
            }
        }
        let isClosed: Bool = lock.withLock {
            if !closed { sentMessages.append(text) }
            return closed
        }
        if isClosed { throw ProviderError.notConnected }
        for event in reply(text) { deliver(event) }
    }

    func receive() async throws -> String {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<String, Error>) in
            lock.lock()
            if ignoresCancel { hung.append(c); lock.unlock(); return }
            if !inbox.isEmpty { let t = inbox.removeFirst(); lock.unlock(); c.resume(returning: t); return }
            if closed { lock.unlock(); c.resume(throwing: ProviderError.notConnected); return }
            waiter = c
            lock.unlock()
        }
    }

    func close() {
        let (w, g): (CheckedContinuation<String, Error>?, CheckedContinuation<Void, Never>?) = lock.withLock {
            closed = true
            closes += 1
            let w = waiter, g = gate
            waiter = nil; gate = nil
            return (w, g)
        }
        w?.resume(throwing: ProviderError.notConnected)
        g?.resume()
    }
}

/// The `gpt-transcribe` recovery stand-in: counts calls, never touches the network.
final class FakeRecovery: DictationProvider, @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    var calls: Int { lock.withLock { n } }
    func finish(_ recording: Recording) async throws -> DictationResult {
        lock.withLock { n += 1 }
        return DictationResult(text: "recovered", modelUsed: "gpt-transcribe")
    }
    func cancel() {}
}
