import Foundation
import Testing
import WisprAltCore

@Suite struct OpenAIRealtimeProviderTests {
    private let live = OpenAIModels.model(id: "gpt-live-transcribe")!
    private let recording = Recording(fileURL: URL(fileURLWithPath: "/dev/null"), durationSeconds: 1,
                                      interruptedByDeviceChange: false)
    private static let committed = #"{"type":"input_audio_buffer.committed","item_id":"item_1"}"#
    private static let completed = #"{"type":"conversation.item.input_audio_transcription.completed","item_id":"item_1","#
        + #""transcript":"hello there","usage":{"type":"tokens","output_tokens":4}}"#

    /// Answers `commit` with `committed` + `completed` (plus `extra` per message).
    private static func happyReply(_ msg: String) -> [String] {
        msg == RealtimeProtocol.commit ? [committed, completed] : []
    }

    private func provider(_ t: FakeTransport, recovery: FakeRecovery? = FakeRecovery(), enabled: Bool = true,
                          drain: Double = 2, commit: Double = 2) -> OpenAIRealtimeProvider {
        OpenAIRealtimeProvider(model: live, keyProvider: { "sk-test" }, fileRecovery: recovery, recoveryEnabled: enabled,
                               makeTransport: { _ in t }, drainSeconds: drain, commitDeadline: { _ in commit })
    }

    private func samples(_ n: Int, value: Float = 0.25) -> [Float] { [Float](repeating: value, count: n) }

    private func waitUntil(_ condition: () -> Bool) async {
        for _ in 0..<300 where !condition() { try? await Task.sleep(nanoseconds: 10_000_000) }
    }

    /// First PCM16 sample of an `input_audio_buffer.append` message.
    private func firstSample(_ message: String) -> Int16? {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(message.utf8)) as? [String: Any],
              let b64 = obj["audio"] as? String, let pcm = Data(base64Encoded: b64), pcm.count >= 2 else { return nil }
        return Int16(bitPattern: UInt16(pcm[0]) | UInt16(pcm[1]) << 8)
    }

    @Test func sessionUpdate_isFirst_thenPreOpenFramesInOrder() async throws {
        let t = FakeTransport(gated: true)
        let p = provider(t)
        p.start()
        for v: Float in [0.1, 0.2, 0.3] { p.append(samples(4_800, value: v)) }
        #expect(t.sent.isEmpty)                                   // still opening
        t.openGate()
        await waitUntil { t.sent.count == 4 }
        #expect(t.sent.first == RealtimeProtocol.sessionUpdate(model: "gpt-live-transcribe"))
        #expect(t.sent.dropFirst().map(firstSample) == [Int16(0.1 * 32767), Int16(0.2 * 32767), Int16(0.3 * 32767)])
        p.cancel()
    }

    @Test func framesReachTheTransport_whileFNIsHeld() async {
        let t = FakeTransport()
        let p = provider(t)
        p.start()
        p.append(samples(2_400))
        p.append(samples(2_400))                                  // 4 800 = one 200 ms message
        await waitUntil { t.sent.count == 2 }
        #expect(t.sent.count == 2)                                // before finish is called
        p.cancel()
    }

    @Test func happyPath_commitsAndReturnsTheCompletedItem() async throws {
        let t = FakeTransport(reply: Self.happyReply)
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery)
        p.start()
        p.append(samples(4_800, value: 0.1))
        p.append(samples(2_000, value: 0.2))                      // partial: sent at finish
        let r = try await p.finish(recording)
        #expect(r == DictationResult(text: "hello there", modelUsed: "gpt-live-transcribe", outputTokens: 4))
        #expect(t.sent.count == 4)
        #expect(t.sent.dropFirst().prefix(2).map(firstSample) == [Int16(0.1 * 32767), Int16(0.2 * 32767)])
        #expect(t.sent.last == RealtimeProtocol.commit)
        #expect(recovery.calls == 0)
        #expect(t.closeCount >= 1)
    }

    @Test func errorReplyToSessionUpdate_nonRecoverable_throws() async {
        let t = FakeTransport(reply: { msg in
            msg.contains("session.update") ? [#"{"type":"error","error":{"type":"invalid_request_error","code":"model_not_found"}}"#] : []
        })
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery)
        p.start()
        p.append(samples(4_800))
        await #expect(throws: ProviderError.realtime(type: "model_not_found")) { try await p.finish(recording) }
        #expect(!t.sent.contains(RealtimeProtocol.commit))
        #expect(recovery.calls == 0)
        #expect(t.closeCount >= 1)
    }

    @Test func serverErrorReply_recovers() async throws {
        let t = FakeTransport(reply: { msg in
            msg.contains("session.update") ? [#"{"type":"error","error":{"type":"server_error"}}"#] : []
        })
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery)
        p.start()
        p.append(samples(4_800))
        await waitUntil { t.sent.count >= 1 }
        let r = try await p.finish(recording)
        #expect(r.modelUsed == "gpt-transcribe")
        #expect(recovery.calls == 1)
    }

    @Test func errorAfterCommit_nonRecoverable_throws() async {
        let t = FakeTransport(reply: { msg in
            msg == RealtimeProtocol.commit ? [#"{"type":"error","error":{"code":"insufficient_quota"}}"#] : []
        })
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery)
        p.start()
        p.append(samples(4_800))
        await #expect(throws: ProviderError.realtime(type: "insufficient_quota")) { try await p.finish(recording) }
        #expect(recovery.calls == 0)
    }

    @Test func bufferOverflow_isNotConnected_andRecovers() async throws {
        let t = FakeTransport(gated: true)                        // never opens while FN is held
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery, drain: 0.2)
        p.start()
        for _ in 0..<52 { p.append(samples(4_800)) }              // 249 600 > 240 000
        let r = try await p.finish(recording)
        #expect(r.modelUsed == "gpt-transcribe")
        #expect(recovery.calls == 1)
        #expect(t.closeCount >= 1)
    }

    @Test func socketNeverOpens_recovers() async throws {
        let t = FakeTransport(openFails: true)
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery)
        p.start()
        p.append(samples(4_800))
        let r = try await p.finish(recording)
        #expect(r == DictationResult(text: "recovered", modelUsed: "gpt-transcribe"))
        #expect(!t.sent.contains(RealtimeProtocol.commit))
        #expect(t.closeCount >= 1)
    }

    @Test func socketNeverOpens_recoveryOff_throwsNotConnected() async {
        let t = FakeTransport(openFails: true)
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery, enabled: false)
        p.start()
        p.append(samples(4_800))
        await #expect(throws: ProviderError.notConnected) { try await p.finish(recording) }
        #expect(recovery.calls == 0)
    }

    @Test func receiveIgnoringCancellation_hitsTheDeadline_recovers_andCloses() async throws {
        let t = FakeTransport(ignoresCancel: true)
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery, commit: 0.2)
        p.start()
        p.append(samples(4_800))
        let r = try await p.finish(recording)
        #expect(r.modelUsed == "gpt-transcribe")
        #expect(t.sent.last == RealtimeProtocol.commit)
        #expect(t.closeCount >= 1)
    }

    @Test func under100ms_isEmptyRecording_noCommit_noRecovery() async {
        let t = FakeTransport(reply: Self.happyReply)
        let recovery = FakeRecovery()
        let p = provider(t, recovery: recovery)
        p.start()
        p.append(samples(2_399))
        await #expect(throws: ProviderError.emptyRecording) { try await p.finish(recording) }
        #expect(!t.sent.contains(RealtimeProtocol.commit))
        #expect(recovery.calls == 0)
        #expect(t.closeCount >= 1)
    }

    @Test func cancel_closes() {
        let t = FakeTransport(gated: true)
        let p = provider(t)
        p.start()
        p.cancel()
        #expect(t.closeCount >= 1)
    }

    @Test func recoveryTable() {
        #expect(RealtimeRecovery.shouldRecover(ProviderError.notConnected))
        #expect(RealtimeRecovery.shouldRecover(ProviderError.deadlineExceeded))
        #expect(RealtimeRecovery.shouldRecover(ProviderError.realtime(type: "server_error")))
        #expect(RealtimeRecovery.shouldRecover(URLError(.networkConnectionLost)))
        #expect(RealtimeRecovery.shouldRecover(ProviderError.http(status: 503, kind: .server)))
        #expect(!RealtimeRecovery.shouldRecover(ProviderError.emptyRecording))
        #expect(!RealtimeRecovery.shouldRecover(ProviderError.cancelled))
        #expect(!RealtimeRecovery.shouldRecover(ProviderError.keyMissing))
        #expect(!RealtimeRecovery.shouldRecover(ProviderError.http(status: 401, kind: .auth)))
        for code in ["invalid_api_key", "insufficient_quota", "model_not_found"] {
            #expect(!RealtimeRecovery.shouldRecover(ProviderError.realtime(type: code)), "\(code)")
        }
        #expect(!RealtimeRecovery.shouldRecover(URLError(.cancelled)))
        #expect(!RealtimeRecovery.shouldRecover(CancellationError()))
    }
}
