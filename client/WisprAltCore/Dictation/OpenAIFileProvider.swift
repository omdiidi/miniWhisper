import Foundation

/// Transcribes a finished recording with one `POST /v1/audio/transcriptions`
/// (16 kHz mono WAV). One retry with backoff on a transient error; permanent
/// errors throw typed `ProviderError.http`. The whole call runs under a
/// deadline of `max(30, 10 + 0.5 × duration)` s whose timeout cancels the
/// in-flight `URLSessionTask`.
public final class OpenAIFileProvider: DictationProvider, @unchecked Sendable {
    public let model: OpenAIModel
    private let keyProvider: @Sendable () -> String?
    private let session: URLSession
    private let deadline: @Sendable (Double) -> Double
    private let retryDelay: Double
    private let inFlight = URLTaskHolder()

    public static func defaultDeadline(_ durationSeconds: Double) -> Double { max(30, 10 + 0.5 * durationSeconds) }

    public init(model: OpenAIModel, keyProvider: @escaping @Sendable () -> String?, session: URLSession = .shared,
                deadline: @escaping @Sendable (Double) -> Double = OpenAIFileProvider.defaultDeadline,
                retryDelay: Double = 1.0) {
        self.model = model
        self.keyProvider = keyProvider
        self.session = session
        self.deadline = deadline
        self.retryDelay = retryDelay
    }

    public func finish(_ recording: Recording) async throws -> DictationResult {
        guard let key = keyProvider(), !key.isEmpty else { throw ProviderError.keyMissing }
        let audio = try Mono16kWAV.make(from: recording.fileURL)
        let request = OpenAIRequests.transcription(model: model, audio: audio, filename: "audio.wav",
                                                   contentType: "audio/wav", prompt: nil,
                                                   responseFormat: "json", key: key)
        let holder = inFlight, session = session, modelID = model.id, delay = retryDelay
        return try await withDeadline(seconds: deadline(recording.durationSeconds), onTimeout: { holder.cancel() }) {
            do {
                return try await Self.send(request, session: session, holder: holder, modelID: modelID)
            } catch let error where OpenAIErrors.isTransient(error) {
                try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                return try await Self.send(request, session: session, holder: holder, modelID: modelID)
            }
        }
    }

    public func cancel() { inFlight.cancel() }

    private static func send(_ request: URLRequest, session: URLSession, holder: URLTaskHolder,
                             modelID: String) async throws -> DictationResult {
        let (data, response) = try await holder.run(session, request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(http.statusCode) else {
            throw ProviderError.http(status: http.statusCode, kind: OpenAIErrors.kind(status: http.statusCode, body: data))
        }
        let t = try OpenAIRequests.decodeTranscription(data)
        return DictationResult(text: t.text, modelUsed: modelID, outputTokens: t.outputTokens)
    }
}

/// Runs data tasks and lets another thread cancel the current one. Once
/// cancelled it stays cancelled: a later task is cancelled as it starts.
final class URLTaskHolder: @unchecked Sendable {
    private let lock = NSLock()
    private var task: URLSessionTask?
    private var cancelled = false

    func run(_ session: URLSession, _ request: URLRequest) async throws -> (Data, URLResponse) {
        try await withCheckedThrowingContinuation { c in
            let t = session.dataTask(with: request) { data, response, error in
                if let error { c.resume(throwing: error) }
                else if let data, let response { c.resume(returning: (data, response)) }
                else { c.resume(throwing: URLError(.badServerResponse)) }
            }
            lock.lock()
            task = t
            let alreadyCancelled = cancelled
            lock.unlock()
            if alreadyCancelled { t.cancel() }
            t.resume()
        }
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let t = task
        lock.unlock()
        t?.cancel()
    }
}
