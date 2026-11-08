import Foundation

/// Builds and decodes OpenAI audio transcription requests. The key goes only
/// into the `Authorization: Bearer` header, never into the URL or the body.
/// Neither `language` nor `languages` is ever sent (A3).
public enum OpenAIRequests {
    public static let transcriptionsURL = URL(string: "https://api.openai.com/v1/audio/transcriptions")!

    /// Multipart `POST /v1/audio/transcriptions`. `prompt` is sent only when
    /// the model supports one. Dictation uses `audio.wav`, `audio/wav`, `json`.
    public static func transcription(model: OpenAIModel, audio: Data, filename: String, contentType: String,
                                     prompt: String?, responseFormat: String, key: String) -> URLRequest {
        let boundary = "wispralt-\(UUID().uuidString)"
        var fields: [(String, String)] = [("model", model.id), ("response_format", responseFormat)]
        if let prompt, model.supportsPrompt { fields.append(("prompt", prompt)) }

        var body = Data()
        for (name, value) in fields {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
        }
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".utf8))
        body.append(Data("Content-Type: \(contentType)\r\n\r\n".utf8))
        body.append(audio)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))

        var request = URLRequest(url: transcriptionsURL)
        request.httpMethod = "POST"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        return request
    }

    public struct Transcription: Equatable, Sendable {
        public let text: String
        public let outputTokens: Int?

        public init(text: String, outputTokens: Int?) {
            self.text = text
            self.outputTokens = outputTokens
        }
    }

    private struct Response: Decodable {
        struct Usage: Decodable { let output_tokens: Int? }
        let text: String
        let usage: Usage?
    }

    /// Decodes a `json` response: `text` plus `usage.output_tokens` when present.
    public static func decodeTranscription(_ data: Data) throws -> Transcription {
        let r = try JSONDecoder().decode(Response.self, from: data)
        return Transcription(text: r.text, outputTokens: r.usage?.output_tokens)
    }
}
