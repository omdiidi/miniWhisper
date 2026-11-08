import Foundation

/// When a failed live session re-sends the same audio to `gpt-transcribe`:
/// socket failures, transient HTTP / server errors and deadlines. Never for an
/// empty recording, a cancel, or a key / quota / model error.
public enum RealtimeRecovery {
    public static func shouldRecover(_ error: Error) -> Bool {
        switch error as? ProviderError {
        case .notConnected?, .deadlineExceeded?: return true
        case .realtime(let type)?: return type == "server_error"
        case .http(_, let kind)?: return kind.isTransient
        case .some: return false
        case nil: break
        }
        if let url = error as? URLError { return url.code != .cancelled }
        return false
    }
}

/// A live-model dictation over the Realtime API. `start()` (at FN press) opens
/// the socket and sends `session.update`; `append` streams 200 ms messages
/// while FN is held, queued in order until the socket is open (at most 10 s
/// waiting, else `.notConnected`; the spool keeps every frame regardless).
/// `finish` drains (5 s), commits and waits for that item's `completed` under
/// `max(15, 5 + 0.25 × duration)` s. A recoverable failure re-sends the
/// recording through `fileRecovery` when `recoveryEnabled`. The socket is
/// closed on every path.
public final class OpenAIRealtimeProvider: DictationProvider, LiveFrameSink, @unchecked Sendable {
    public static let samplesPerMessage = 4_800      // 200 ms at 24 kHz
    public static let maxQueuedSamples = 240_000     // 10 s
    public static let minSamples = 2_400             // 100 ms
    public static func defaultCommitDeadline(_ durationSeconds: Double) -> Double { max(15, 5 + 0.25 * durationSeconds) }

    public let model: OpenAIModel
    public let inputSampleRate = RealtimeProtocol.sampleRate
    public let recoveryEnabled: Bool
    private let keyProvider: @Sendable () -> String?
    private let makeTransport: @Sendable (URLRequest) -> WebSocketTransport
    private let fileRecovery: DictationProvider?
    private let drainSeconds: Double
    private let commitDeadline: @Sendable (Double) -> Double

    private struct Message: Sendable { let text: String; let samples: Int }
    private let outgoing: AsyncStream<Message>
    private let outgoingInput: AsyncStream<Message>.Continuation
    private let events: AsyncStream<RealtimeProtocol.ServerEvent>
    private let eventsInput: AsyncStream<RealtimeProtocol.ServerEvent>.Continuation

    private let lock = NSLock()
    private var started = false
    private var transport: WebSocketTransport?
    private var sender: Task<Void, Never>?
    private var receiver: Task<Void, Never>?
    private var pending: [Float] = []
    private var appended = 0
    private var queued = 0
    private var failure: Error?

    public init(model: OpenAIModel, keyProvider: @escaping @Sendable () -> String?,
                fileRecovery: DictationProvider?, recoveryEnabled: Bool = true,
                makeTransport: @escaping @Sendable (URLRequest) -> WebSocketTransport = { URLSessionWebSocketTransport(request: $0) },
                drainSeconds: Double = 5,
                commitDeadline: @escaping @Sendable (Double) -> Double = OpenAIRealtimeProvider.defaultCommitDeadline) {
        self.model = model
        self.keyProvider = keyProvider
        self.fileRecovery = fileRecovery
        self.recoveryEnabled = recoveryEnabled
        self.makeTransport = makeTransport
        self.drainSeconds = drainSeconds
        self.commitDeadline = commitDeadline
        (outgoing, outgoingInput) = AsyncStream.makeStream()
        (events, eventsInput) = AsyncStream.makeStream()
    }

    // MARK: LiveFrameSink

    public func start() {
        let alreadyStarted: Bool = lock.withLock { let was = started; started = true; return was }
        guard !alreadyStarted else { return }
        guard let key = keyProvider(), !key.isEmpty else { return fail(ProviderError.keyMissing) }
        let t = makeTransport(RealtimeProtocol.request(key: key))
        let task = Task { await self.runSender(t) }
        lock.withLock { transport = t; sender = task }
    }

    public func append(_ mono: [Float]) {
        lock.lock(); defer { lock.unlock() }
        appended += mono.count
        guard failure == nil else { return }
        pending.append(contentsOf: mono)
        var from = 0
        while pending.count - from >= Self.samplesPerMessage {
            enqueueLocked(pending[from..<(from + Self.samplesPerMessage)])
            from += Self.samplesPerMessage
        }
        pending.removeFirst(from)
    }

    private func enqueueLocked(_ samples: ArraySlice<Float>) {
        guard failure == nil else { return }
        guard queued + samples.count <= Self.maxQueuedSamples else {
            failure = ProviderError.notConnected
            return
        }
        queued += samples.count
        outgoingInput.yield(Message(text: RealtimeProtocol.append(samples), samples: samples.count))
    }

    // MARK: Socket tasks (one sender, one receiver)

    private func runSender(_ t: WebSocketTransport) async {
        do {
            try await t.send(RealtimeProtocol.sessionUpdate(model: model.id))
            let task = Task { await self.runReceiver(t) }
            lock.withLock { receiver = task }
            for await message in outgoing {
                if lock.withLock({ failure != nil }) { break }
                try await t.send(message.text)
                lock.withLock { queued -= message.samples }
            }
        } catch {
            fail(error)
        }
    }

    private func runReceiver(_ t: WebSocketTransport) async {
        do {
            while true {
                let event = RealtimeProtocol.decode(try await t.receive())
                if case .error(let code) = event { fail(ProviderError.realtime(type: code)) }
                eventsInput.yield(event)
            }
        } catch {
            fail(error)
            eventsInput.finish()
        }
    }

    private func fail(_ error: Error) {
        lock.withLock { if failure == nil { failure = error } }
    }

    // MARK: DictationProvider

    public func finish(_ recording: Recording) async throws -> DictationResult {
        defer { close() }
        do {
            return try await finishLive(recording)
        } catch let error where recoveryEnabled && RealtimeRecovery.shouldRecover(error) {
            guard let fileRecovery else { throw error }
            close()
            return try await fileRecovery.finish(recording)
        }
    }

    private func finishLive(_ recording: Recording) async throws -> DictationResult {
        let (total, sender, transport): (Int, Task<Void, Never>?, WebSocketTransport?) = lock.withLock {
            if !pending.isEmpty { enqueueLocked(pending[...]); pending = [] }
            outgoingInput.finish()
            return (appended, self.sender, self.transport)
        }
        guard total >= Self.minSamples else { throw ProviderError.emptyRecording }
        guard let sender, let transport else { throw currentFailure() ?? ProviderError.notConnected }

        try await withDeadline(seconds: drainSeconds, onTimeout: { transport.close() }) { await sender.value }
        if let failure = currentFailure() { throw failure }

        let events = events, modelID = model.id
        return try await withDeadline(seconds: commitDeadline(recording.durationSeconds),
                                      onTimeout: { transport.close() }) { [self] in
            try await transport.send(RealtimeProtocol.commit)
            var committed: String?
            var completed: [String: DictationResult] = [:]
            for await event in events {
                switch event {
                case .committed(let id):
                    committed = id
                    if let r = completed[id] { return r }
                case .completed(let id, let text, let tokens):
                    let r = DictationResult(text: text, modelUsed: modelID, outputTokens: tokens)
                    if id == committed { return r }
                    completed[id] = r
                case .failed(let id, let code) where committed == nil || id == committed:
                    throw ProviderError.realtime(type: code)
                case .error(let code):
                    throw ProviderError.realtime(type: code)
                default:
                    continue
                }
            }
            throw currentFailure() ?? ProviderError.notConnected   // socket closed before `completed`
        }
    }

    private func currentFailure() -> Error? { lock.withLock { failure } }

    public func cancel() {
        fail(ProviderError.cancelled)
        close()
    }

    private func close() {
        let (t, s, r) = lock.withLock { (transport, sender, receiver) }
        outgoingInput.finish()
        t?.close()
        s?.cancel()
        r?.cancel()
    }
}
