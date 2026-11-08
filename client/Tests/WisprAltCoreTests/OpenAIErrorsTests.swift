import Foundation
import Testing
import WisprAltCore

@Suite struct OpenAIErrorsTests {
    private func body(code: String?, type: String? = nil) -> Data {
        let c = code.map { "\"\($0)\"" } ?? "null"
        let t = type.map { "\"\($0)\"" } ?? "null"
        return Data(#"{"error":{"message":"m","code":\#(c),"type":\#(t)}}"#.utf8)
    }

    private func transient(_ status: Int, _ body: Data? = nil) -> Bool {
        OpenAIErrors.isTransient(ProviderError.http(status: status, kind: OpenAIErrors.kind(status: status, body: body)))
    }

    @Test func permanent_kinds() {
        #expect(OpenAIErrors.kind(status: 401, body: nil) == .auth)
        #expect(OpenAIErrors.kind(status: 403, body: nil) == .auth)
        #expect(OpenAIErrors.kind(status: 400, body: nil) == .badRequest)
        #expect(OpenAIErrors.kind(status: 404, body: body(code: "model_not_found")) == .modelNotFound)
        #expect(OpenAIErrors.kind(status: 400, body: body(code: "model_not_found")) == .modelNotFound)
        #expect(OpenAIErrors.kind(status: 429, body: body(code: "insufficient_quota")) == .quota)
        #expect(OpenAIErrors.kind(status: 429, body: body(code: nil, type: "insufficient_quota")) == .quota)
        for status in [401, 403, 400] { #expect(!transient(status), "\(status)") }
        #expect(!transient(404, body(code: "model_not_found")))
        #expect(!transient(429, body(code: "insufficient_quota")))
    }

    @Test func transient_kinds() {
        #expect(OpenAIErrors.kind(status: 429, body: body(code: "rate_limit_exceeded")) == .rateLimited)
        #expect(transient(429))
        for status in [500, 502, 503, 504] { #expect(transient(status), "\(status)") }
        #expect(OpenAIErrors.isTransient(URLError(.timedOut)))
        #expect(OpenAIErrors.isTransient(URLError(.notConnectedToInternet)))
        #expect(!OpenAIErrors.isTransient(URLError(.cancelled)))
        #expect(!OpenAIErrors.isTransient(ProviderError.deadlineExceeded))
    }

    @Test func failureKinds() {
        #expect(ProviderError.http(status: 401, kind: .auth).outcomeKind == .failed(.auth))
        #expect(ProviderError.http(status: 429, kind: .quota).outcomeKind == .failed(.quota))
        #expect(ProviderError.http(status: 404, kind: .modelNotFound).outcomeKind == .failed(.modelNotFound))
        #expect(ProviderError.http(status: 503, kind: .server).outcomeKind == .failed(.server))
    }
}
