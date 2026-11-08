import Foundation
import Testing
import WisprAltCore

/// Uses the static `StubURLProtocol`, so it is nested in the serialized `StubURLSuites`.
extension StubURLSuites {
    @Suite struct OpenAIFileProviderTests {
        private let ok = StubURLProtocol.Reply.http(200, Data(#"{"text":"hello world","usage":{"output_tokens":3}}"#.utf8))

        private func provider(key: String? = "sk-test", deadline: Double = 10) -> OpenAIFileProvider {
            OpenAIFileProvider(model: OpenAIModels.resolve(id: nil), keyProvider: { key },
                               session: StubURLProtocol.session(), deadline: { _ in deadline }, retryDelay: 0.01)
        }

        private func recording() throws -> Recording {
            Recording(fileURL: try writeTestRecording(seconds: 0.5), durationSeconds: 0.5, interruptedByDeviceChange: false)
        }

        @Test func ok200_returnsText_andUsage() async throws {
            StubURLProtocol.reset([ok])
            let rec = try recording()
            defer { try? FileManager.default.removeItem(at: rec.fileURL) }
            let result = try await provider().finish(rec)
            #expect(result == DictationResult(text: "hello world", modelUsed: "gpt-transcribe", outputTokens: 3))
            #expect(StubURLProtocol.requests.count == 1)
            let sent = StubURLProtocol.requests[0]
            #expect(sent.value(forHTTPHeaderField: "Authorization") == "Bearer sk-test")
            let body = sent.httpBody ?? Data()
            #expect(body.range(of: Data("filename=\"audio.wav\"".utf8)) != nil)
        }

        @Test func transient503_thenOK_retriesOnce() async throws {
            StubURLProtocol.reset([.http(503, Data()), ok])
            let rec = try recording()
            defer { try? FileManager.default.removeItem(at: rec.fileURL) }
            let result = try await provider().finish(rec)
            #expect(result.text == "hello world")
            #expect(StubURLProtocol.requests.count == 2)
        }

        @Test func permanent401_noRetry() async throws {
            StubURLProtocol.reset([.http(401, Data(#"{"error":{"code":"invalid_api_key"}}"#.utf8)), ok])
            let rec = try recording()
            defer { try? FileManager.default.removeItem(at: rec.fileURL) }
            await #expect(throws: ProviderError.http(status: 401, kind: .auth)) { try await provider().finish(rec) }
            #expect(StubURLProtocol.requests.count == 1)
        }

        @Test func missingKey_throwsKeyMissing_noRequest() async throws {
            StubURLProtocol.reset([ok])
            let rec = try recording()
            defer { try? FileManager.default.removeItem(at: rec.fileURL) }
            await #expect(throws: ProviderError.keyMissing) { try await provider(key: nil).finish(rec) }
            #expect(StubURLProtocol.requests.isEmpty)
        }

        @Test func neverResponding_hitsDeadline_andCancelsTheTask() async throws {
            StubURLProtocol.reset([.hang])
            let rec = try recording()
            defer { try? FileManager.default.removeItem(at: rec.fileURL) }
            let start = Date()
            await #expect(throws: ProviderError.deadlineExceeded) { try await provider(deadline: 0.3).finish(rec) }
            #expect(Date().timeIntervalSince(start) < 2)
            // stopLoading runs once the cancelled task is torn down.
            for _ in 0..<50 where StubURLProtocol.stopLoadingCount == 0 { try await Task.sleep(nanoseconds: 20_000_000) }
            #expect(StubURLProtocol.stopLoadingCount >= 1)
        }

        @Test func defaultDeadline_isMax30_or10PlusHalfDuration() {
            #expect(OpenAIFileProvider.defaultDeadline(10) == 30)
            #expect(OpenAIFileProvider.defaultDeadline(600) == 310)
        }
    }
}
