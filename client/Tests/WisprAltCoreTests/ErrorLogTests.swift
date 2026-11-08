import Foundation
import Testing
import WisprAltCore

private struct FakeSafeError: LogSafeError {
    var logDescription: String { "fake(safe)" }
    var outcomeKind: ErrorOutcome { .failed(.network) }
}

/// Stand-in for an app error that carries a response body.
private struct FakeBodyError: LogSafeError, LocalizedError {
    let status: Int
    let body: String
    var logDescription: String { "server(status: \(status))" }
    var outcomeKind: ErrorOutcome { .failed(.server) }
    var errorDescription: String? { "Server error \(status): \(body)" }
}

@Suite struct ErrorLogTests {
    @Test func logSafeError_usesItsOwnDescriptionAndOutcome() {
        #expect(ErrorLog.describe(FakeSafeError()) == "fake(safe)")
        #expect(ErrorLog.outcome(FakeSafeError()) == .failed(.network))
    }

    @Test func bodyCarryingError_neverLeaksTheBody() {
        let e = FakeBodyError(status: 500, body: "secret words")
        #expect(ErrorLog.describe(e) == "server(status: 500)")
        #expect(!ErrorLog.describe(e).contains("secret"))
    }

    @Test func plainNSError_isTypeDomainCode_withoutItsMessage() {
        let e = NSError(domain: "X", code: 7, userInfo: [NSLocalizedDescriptionKey: "secret words"])
        #expect(ErrorLog.describe(e) == "NSError X#7")
        #expect(ErrorLog.outcome(e) == .failed(.other))
    }

    @Test func cancellation_isCancelled() {
        #expect(ErrorLog.outcome(CancellationError()) == .cancelled)
    }

    @Test func providerErrors() {
        #expect(ProviderError.emptyRecording.outcomeKind == .empty)
        #expect(ErrorLog.outcome(ProviderError.keyMissing) == .failed(.keyMissing))
        #expect(ErrorLog.outcome(ProviderError.deadlineExceeded) == .failed(.deadline))
        #expect(ErrorLog.describe(ProviderError.http(status: 401, kind: .auth)) == "http(status: 401, kind: auth)")
        #expect(ErrorLog.outcome(ProviderError.notConnected) == .failed(.network))
        #expect(ErrorLog.describe(ProviderError.realtime(type: "server_error")) == "realtime(type: server_error)")
        #expect(ErrorLog.outcome(ProviderError.realtime(type: "invalid_api_key")) == .failed(.auth))
        #expect(ErrorLog.outcome(ProviderError.realtime(type: "insufficient_quota")) == .failed(.quota))
        #expect(ErrorLog.outcome(ProviderError.realtime(type: "model_not_found")) == .failed(.modelNotFound))
        #expect(ErrorLog.outcome(ProviderError.realtime(type: "server_error")) == .failed(.server))
        #expect(ErrorLog.outcome(ProviderError.realtime(type: "invalid_value")) == .failed(.other))
    }
}
