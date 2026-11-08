import Foundation

/// Mercury 2 smart formatting on OpenRouter, run by the client after
/// transcription (both modes, opt-in). Sends **text only**. Fail-soft: every
/// failure returns the raw text with a `Formatting` reason; it never throws.
public final class MercuryClient: @unchecked Sendable {
    public static let endpoint = URL(string: "https://openrouter.ai/api/v1/chat/completions")!
    public static let model = "inception/mercury-2"

    private let keyProvider: @Sendable () -> String?
    private let session: URLSession
    private let timeout: Double

    public init(keyProvider: @escaping @Sendable () -> String?, session: URLSession = .shared, timeout: Double = 2.5) {
        self.keyProvider = keyProvider
        self.session = session
        self.timeout = timeout
    }

    public func format(_ raw: String) async -> (text: String, formatting: Formatting) {
        guard MercuryPolicy.wordCount(raw) >= MercuryPolicy.minWords else { return (raw, .skipped("short")) }
        guard let key = keyProvider(), !key.isEmpty else { return (raw, .skipped("no key")) }
        let request = Self.request(raw, key: key)
        let holder = URLTaskHolder(), session = session
        do {
            let (data, response) = try await withDeadline(seconds: timeout, onTimeout: { holder.cancel() }) {
                try await holder.run(session, request)
            }
            guard let status = (response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status) else {
                return (raw, .failed("http"))
            }
            let cleaned = Self.cleanedText(data).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cleaned.isEmpty else { return (raw, .failed("empty")) }
            guard MercuryPolicy.withinWindow(raw: raw, cleaned: cleaned) else { return (raw, .failed("ratio")) }
            return (cleaned, .applied)
        } catch ProviderError.deadlineExceeded {
            return (raw, .failed("timeout"))
        } catch {
            return (raw, .failed("network"))
        }
    }

    static func request(_ raw: String, key: String) -> URLRequest {
        var r = URLRequest(url: endpoint)
        r.httpMethod = "POST"
        r.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        r.setValue("WisprAlt", forHTTPHeaderField: "X-Title")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Any] = [
            "model": model,
            "messages": [["role": "system", "content": MercuryPolicy.systemPrompt],
                         ["role": "user", "content": raw]],
            // Scales with input so long dictations are not cut mid-sentence.
            "max_tokens": min(max(raw.count / 2, 256), 4096),
            "temperature": 0.0,
            // Mercury 2 otherwise routes output through `reasoning`.
            "reasoning": ["enabled": false],
        ]
        r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        return r
    }

    /// `choices[0].message.content`, else `.reasoning` (provider quirk).
    static func cleanedText(_ data: Data) -> String {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let message = (json["choices"] as? [[String: Any]])?.first?["message"] as? [String: Any]
        else { return "" }
        let content = MercuryPolicy.extractText(message["content"])
        return content.isEmpty ? MercuryPolicy.extractText(message["reasoning"]) : content
    }
}
