import Foundation

/// OpenAI Realtime transcription wire format (live models, Task 11).
/// `wss://api.openai.com/v1/realtime?intent=transcription`: no `model` query
/// parameter and no `OpenAI-Beta` header; the model goes in `session.update`.
/// Audio is 24 kHz mono PCM16 little-endian, base64 in `input_audio_buffer.append`.
public enum RealtimeProtocol {
    public static let url = URL(string: "wss://api.openai.com/v1/realtime?intent=transcription")!
    public static let sampleRate = 24_000

    /// The WebSocket upgrade request. The key goes only into the Bearer header.
    public static func request(key: String) -> URLRequest {
        var r = URLRequest(url: url)
        r.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        return r
    }

    /// First message on every socket: a transcription session for `model`
    /// with manual commits (`turn_detection: null`).
    public static func sessionUpdate(model: String) -> String {
        #"{"type":"session.update","session":{"type":"transcription","audio":{"input":{"format":"#
            + #"{"type":"audio/pcm","rate":24000},"transcription":{"model":"\#(model)"},"turn_detection":null}}}}"#
    }

    /// One `input_audio_buffer.append`: samples clamped to [-1, 1], PCM16 LE, base64.
    public static func append(_ samples: ArraySlice<Float>) -> String {
        var pcm = Data(count: samples.count * 2)
        pcm.withUnsafeMutableBytes { raw in
            let out = raw.bindMemory(to: UInt8.self)
            for (i, s) in samples.enumerated() {
                let v = Int16(max(-1, min(1, s)) * 32767)
                let u = UInt16(bitPattern: v)
                out[2 * i] = UInt8(u & 0xFF)
                out[2 * i + 1] = UInt8(u >> 8)
            }
        }
        return #"{"type":"input_audio_buffer.append","audio":""# + pcm.base64EncodedString() + #""}"#
    }

    public static let commit = #"{"type":"input_audio_buffer.commit"}"#

    /// A server event. `delta` text is decoded but never shown (no interim UI);
    /// error codes are API identifiers, sanitized so they are safe to log.
    public enum ServerEvent: Equatable, Sendable {
        case session(type: String)
        case committed(itemID: String)
        case delta(itemID: String, text: String)
        case completed(itemID: String, transcript: String, outputTokens: Int?)
        case failed(itemID: String, code: String)
        case error(code: String)
        case other(type: String)
    }

    private struct Envelope: Decodable {
        struct Usage: Decodable { let output_tokens: Int? }
        struct Detail: Decodable { let type: String?; let code: String? }
        let type: String
        let item_id: String?
        let delta: String?
        let transcript: String?
        let usage: Usage?
        let error: Detail?
    }

    public static func decode(_ text: String) -> ServerEvent {
        guard let e = try? JSONDecoder().decode(Envelope.self, from: Data(text.utf8)) else {
            return .other(type: "undecodable")
        }
        let item = e.item_id ?? ""
        let code = safeCode(e.error?.code ?? e.error?.type)
        switch e.type {
        case "input_audio_buffer.committed": return .committed(itemID: item)
        case "conversation.item.input_audio_transcription.delta": return .delta(itemID: item, text: e.delta ?? "")
        case "conversation.item.input_audio_transcription.completed":
            return .completed(itemID: item, transcript: e.transcript ?? "", outputTokens: e.usage?.output_tokens)
        case "conversation.item.input_audio_transcription.failed": return .failed(itemID: item, code: code)
        case "error": return .error(code: code)
        case let t where t.hasPrefix("session.") || t.hasPrefix("transcription_session."): return .session(type: t)
        default: return .other(type: safeCode(e.type))
        }
    }

    /// Keeps `[a-z0-9_.]`, at most 64 characters; "unknown" when nothing is left.
    static func safeCode(_ s: String?) -> String {
        let kept = String((s ?? "").lowercased().filter { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "_" || $0 == ".") }
            .prefix(64))
        return kept.isEmpty ? "unknown" : kept
    }
}

/// A text-frame WebSocket. The socket starts connecting when the transport is
/// created; the first `send` completes only once it is open.
public protocol WebSocketTransport: AnyObject, Sendable {
    func send(_ text: String) async throws
    /// The next text frame (binary frames are skipped). Cancelling the caller closes the socket.
    func receive() async throws -> String
    /// Idempotent.
    func close()
}

/// `URLSessionWebSocketTask` transport. A failed upgrade with an HTTP status
/// throws `ProviderError.http`; any other socket failure `.notConnected`.
public final class URLSessionWebSocketTransport: WebSocketTransport, @unchecked Sendable {
    private let session: URLSession
    private let task: URLSessionWebSocketTask

    public init(request: URLRequest) {
        session = URLSession(configuration: .ephemeral)
        task = session.webSocketTask(with: request)
        task.resume()
    }

    public func send(_ text: String) async throws {
        do { try await task.send(.string(text)) } catch { throw mapped(error) }
    }

    public func receive() async throws -> String {
        let task = task
        return try await withTaskCancellationHandler {
            while true {
                let message: URLSessionWebSocketTask.Message
                do { message = try await task.receive() } catch { throw mapped(error) }
                if case .string(let text) = message { return text }
            }
        } onCancel: { [weak self] in
            self?.close()
        }
    }

    public func close() {
        task.cancel(with: .normalClosure, reason: nil)
        session.invalidateAndCancel()
    }

    private func mapped(_ error: Error) -> Error {
        if error is CancellationError { return error }
        if let http = task.response as? HTTPURLResponse, http.statusCode != 101 {
            return ProviderError.http(status: http.statusCode, kind: OpenAIErrors.kind(status: http.statusCode, body: nil))
        }
        return ProviderError.notConnected
    }
}
