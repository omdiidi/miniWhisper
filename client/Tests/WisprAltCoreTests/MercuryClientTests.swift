import Foundation
import Testing
import WisprAltCore

/// Uses the static `StubURLProtocol`, so it is nested in the serialized `StubURLSuites`.
extension StubURLSuites {
    @Suite struct MercuryClientTests {
        private let raw = (1...30).map { "word\($0)" }.joined(separator: " ")

        private func client(key: String? = "or-test", timeout: Double = 2.5) -> MercuryClient {
            MercuryClient(keyProvider: { key }, session: StubURLProtocol.session(), timeout: timeout)
        }

        private func reply(_ content: String) -> StubURLProtocol.Reply {
            let body = try! JSONSerialization.data(withJSONObject: ["choices": [["message": ["content": content]]]])
            return .http(200, body)
        }

        @Test func success_returnsCleaned_andSendsTheContract() async throws {
            let cleaned = raw.capitalized + "."
            StubURLProtocol.reset([reply(cleaned)])
            let (text, formatting) = await client().format(raw)
            #expect(text == cleaned)
            #expect(formatting == .applied)
            let sent = try #require(StubURLProtocol.requests.first)
            #expect(sent.url == MercuryClient.endpoint)
            #expect(sent.value(forHTTPHeaderField: "Authorization") == "Bearer or-test")
            #expect(sent.value(forHTTPHeaderField: "X-Title") == "WisprAlt")
            let body = try #require(try JSONSerialization.jsonObject(with: sent.httpBody ?? Data()) as? [String: Any])
            #expect(body["model"] as? String == "inception/mercury-2")
            let messages = try #require(body["messages"] as? [[String: String]])
            #expect(messages.first?["content"] == MercuryPolicy.systemPrompt)
            #expect(messages.last?["content"] == raw)
        }

        @Test func timeout_failsSoft_withRawText() async {
            StubURLProtocol.reset([.hang])
            let start = Date()
            let (text, formatting) = await client(timeout: 0.2).format(raw)
            #expect(text == raw)
            #expect(formatting == .failed("timeout"))
            #expect(Date().timeIntervalSince(start) < 2)
        }

        @Test func ratio_rejected_keepsRaw() async {
            StubURLProtocol.reset([reply("Short summary.")])
            let (text, formatting) = await client().format(raw)
            #expect(text == raw)
            #expect(formatting == .failed("ratio"))
        }

        @Test func short_skipped_noRequest() async {
            StubURLProtocol.reset([reply("x")])
            let short = "only a few words here"
            let (text, formatting) = await client().format(short)
            #expect(text == short)
            #expect(formatting == .skipped("short"))
            #expect(StubURLProtocol.requests.isEmpty)
        }

        @Test func noKey_skipped_noRequest() async {
            StubURLProtocol.reset([reply("x")])
            let (_, formatting) = await client(key: nil).format(raw)
            #expect(formatting == .skipped("no key"))
            #expect(StubURLProtocol.requests.isEmpty)
        }

        @Test func httpError_and_reasoningFallback() async {
            StubURLProtocol.reset([.http(500, Data())])
            #expect(await client().format(raw).formatting == .failed("http"))
            let body = try! JSONSerialization.data(withJSONObject: ["choices": [["message": ["content": NSNull(), "reasoning": raw]]]])
            StubURLProtocol.reset([.http(200, body)])
            #expect(await client().format(raw) == (raw, .applied))
        }
    }
}
