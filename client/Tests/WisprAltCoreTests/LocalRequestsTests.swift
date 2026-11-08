import Foundation
import Testing
import WisprAltCore

/// Sends the built requests through `StubURLProtocol` and checks what went on
/// the wire. Nested in the serialized `StubURLSuites`.
extension StubURLSuites {
    @Suite struct LocalRequestsTests {
        private let endpoint = ServerEndpoint(baseURL: URL(string: "https://captured.example.com")!, apiKey: "local-key")
        private let dedup = "0F8C2A5E-3B1D-4C7E-9A2F-6D4B8E1C0A77"
        private let wav = Data("RIFFfakewav".utf8)

        /// Sends every request and returns them as the stub recorded them.
        private func send(_ requests: [URLRequest]) async throws -> [URLRequest] {
            StubURLProtocol.reset(requests.map { _ in .http(200, Data(#"{"text":"ok"}"#.utf8)) })
            let session = StubURLProtocol.session()
            for r in requests { _ = try await session.data(for: r) }
            return StubURLProtocol.requests
        }

        private func body(_ r: URLRequest) -> String { String(decoding: r.httpBody ?? Data(), as: UTF8.self) }

        private func all() -> [URLRequest] {
            [
                LocalRequests.chunk(endpoint: endpoint, sessionId: "sid", index: 3, dedupId: dedup, wav: wav,
                                    clientVersion: "1.2+3"),
                LocalRequests.finalize(endpoint: endpoint, sessionId: "sid", dedupId: dedup,
                                       speechStartedAt: Date(timeIntervalSince1970: 1_762_000_000.125),
                                       tail: wav, clientVersion: "1.2+3"),
                LocalRequests.wholeFile(endpoint: endpoint, dedupId: dedup, wav: wav, clientVersion: "1.2+3"),
            ]
        }

        @Test func exactPaths_onTheCapturedEndpoint() async throws {
            let sent = try await send(all())
            #expect(sent.map { $0.url?.absoluteString } == [
                "https://captured.example.com/transcribe/dictate/stream/sid/chunk/3",
                "https://captured.example.com/transcribe/dictate/stream/sid/finalize",
                "https://captured.example.com/transcribe/dictate",
            ])
            #expect(sent.allSatisfy { $0.httpMethod == "POST" })
        }

        @Test func basePathPrefix_isKept() {
            let ep = ServerEndpoint(baseURL: URL(string: "https://host.example.com/wispralt")!, apiKey: nil)
            let r = LocalRequests.wholeFile(endpoint: ep, dedupId: dedup, wav: Data(), clientVersion: "1")
            #expect(r.url?.absoluteString == "https://host.example.com/wispralt/transcribe/dictate")
        }

        @Test func allThree_carryAuthVersionAndLowercaseDedup() async throws {
            for r in try await send(all()) {
                #expect(r.value(forHTTPHeaderField: "X-Smart-Format") == nil)
                #expect(r.value(forHTTPHeaderField: "X-Client-Dedup-Id") == dedup.lowercased())
                #expect(r.value(forHTTPHeaderField: "Authorization") == "Bearer local-key")
                #expect(r.value(forHTTPHeaderField: "X-WisprAlt-Client-Version") == "1.2+3")
                #expect(body(r).contains("name=\"file\""))
            }
        }

        @Test func finalize_carriesItsFields_andNoSmartFormat() async throws {
            let started = Date(timeIntervalSince1970: 1_762_000_000.125)
            let r = try await send([all()[1]])[0]
            let b = body(r)
            #expect(b.contains("name=\"file\"; filename=\"tail.wav\""))
            #expect(b.contains("name=\"client_dedup_id\"\r\n\r\n\(dedup.lowercased())\r\n"))
            #expect(!b.contains("smart_format"))   // Mercury runs in the client since Part 3
            let field = try #require(b.components(separatedBy: "name=\"speech_started_at\"\r\n\r\n").last?
                .components(separatedBy: "\r\n").first)
            #expect(Double(field) == started.timeIntervalSince1970)
        }

        @Test func noKey_noAuthorizationHeader() {
            let open = ServerEndpoint(baseURL: URL(string: "http://127.0.0.1:8000")!, apiKey: nil)
            let r = LocalRequests.wholeFile(endpoint: open, dedupId: dedup, wav: wav, clientVersion: "v")
            #expect(r.value(forHTTPHeaderField: "Authorization") == nil)
            #expect(r.url?.absoluteString == "http://127.0.0.1:8000/transcribe/dictate")
        }

        @Test func decode_savedFlag_defaultsTrue() throws {
            #expect(try LocalRequests.decode(Data(#"{"text":"a","saved":false}"#.utf8)) == .init(text: "a", saved: false))
            #expect(try LocalRequests.decode(Data(#"{"text":"a","saved":true,"replayed":true}"#.utf8)).saved)
            #expect(try LocalRequests.decode(Data(#"{"text":"a"}"#.utf8)).saved)
        }

        @Test func terminalErrors_onlyForTheContractBodies() {
            func t(_ status: Int, _ json: String) -> LocalDictationError? {
                LocalRequests.terminalError(status: status, body: Data(json.utf8))
            }
            #expect(t(410, #"{"error":"dictation_deleted"}"#) == .dictationDeleted)
            #expect(t(409, #"{"error":"dedup_conflict"}"#) == .dedupConflict)
            #expect(t(410, #"{"detail":"Session is not open"}"#) == nil)
            #expect(t(409, #"{"detail":"x"}"#) == nil)
            #expect(t(500, #"{"error":"dictation_deleted"}"#) == nil)
            #expect(t(410, "not json") == nil)
            #expect(ErrorLog.outcome(LocalDictationError.dictationDeleted) == .failed(.rejected))
            #expect(ErrorLog.outcome(LocalDictationError.dedupConflict) == .failed(.rejected))
        }
    }
}
