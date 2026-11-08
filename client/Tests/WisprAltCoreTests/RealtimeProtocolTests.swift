import Foundation
import Testing
@testable import WisprAltCore

@Suite struct RealtimeProtocolTests {
    @Test func sessionUpdate_isByteStable() {
        #expect(RealtimeProtocol.sessionUpdate(model: "gpt-live-transcribe")
            == #"{"type":"session.update","session":{"type":"transcription","audio":{"input":{"format":"#
            + #"{"type":"audio/pcm","rate":24000},"transcription":{"model":"gpt-live-transcribe"},"#
            + #""turn_detection":null}}}}"#)
        let obj = try? JSONSerialization.jsonObject(with: Data(RealtimeProtocol.sessionUpdate(model: "m").utf8))
        #expect(obj != nil)
    }

    @Test func url_hasNoModel_andTheKeyIsOnlyInTheBearerHeader() {
        let r = RealtimeProtocol.request(key: "sk-test")
        #expect(r.url?.absoluteString == "wss://api.openai.com/v1/realtime?intent=transcription")
        #expect(!(r.url?.absoluteString.contains("model") ?? true))
        #expect(r.value(forHTTPHeaderField: "Authorization") == "Bearer sk-test")
        #expect(r.value(forHTTPHeaderField: "OpenAI-Beta") == nil)
    }

    @Test func append_isClampedPCM16LittleEndianBase64() {
        let msg = RealtimeProtocol.append([0, 1, -1, 2][...])
        let expected = Data([0x00, 0x00, 0xFF, 0x7F, 0x01, 0x80, 0xFF, 0x7F]).base64EncodedString()
        #expect(msg == #"{"type":"input_audio_buffer.append","audio":"\#(expected)"}"#)
        #expect(RealtimeProtocol.commit == #"{"type":"input_audio_buffer.commit"}"#)
    }

    @Test func decode_events() {
        typealias E = RealtimeProtocol.ServerEvent
        #expect(RealtimeProtocol.decode(#"{"type":"session.updated","session":{}}"#) == E.session(type: "session.updated"))
        #expect(RealtimeProtocol.decode(#"{"type":"input_audio_buffer.committed","item_id":"a"}"#) == E.committed(itemID: "a"))
        #expect(RealtimeProtocol.decode(#"{"type":"conversation.item.input_audio_transcription.delta","item_id":"a","delta":"he"}"#)
            == E.delta(itemID: "a", text: "he"))
        #expect(RealtimeProtocol.decode(#"{"type":"conversation.item.input_audio_transcription.completed","item_id":"a","transcript":"hi"}"#)
            == E.completed(itemID: "a", transcript: "hi", outputTokens: nil))
        #expect(RealtimeProtocol.decode(#"{"type":"conversation.item.input_audio_transcription.completed","item_id":"a","transcript":"hi","usage":{"type":"tokens","output_tokens":9}}"#)
            == E.completed(itemID: "a", transcript: "hi", outputTokens: 9))
        #expect(RealtimeProtocol.decode(#"{"type":"conversation.item.input_audio_transcription.failed","item_id":"a","error":{"type":"server_error"}}"#)
            == E.failed(itemID: "a", code: "server_error"))
        #expect(RealtimeProtocol.decode(#"{"type":"error","error":{"type":"invalid_request_error","code":"model_not_found","message":"secret"}}"#)
            == E.error(code: "model_not_found"))
        #expect(RealtimeProtocol.decode(#"{"type":"rate_limits.updated"}"#) == E.other(type: "rate_limits.updated"))
        #expect(RealtimeProtocol.decode("not json") == E.other(type: "undecodable"))
    }

    @Test func errorCodes_areSanitizedForLogs() {
        #expect(RealtimeProtocol.safeCode("Bad Code: /Users/x") == "badcodeusersx")
        #expect(RealtimeProtocol.safeCode(nil) == "unknown")
        #expect(RealtimeProtocol.safeCode(String(repeating: "a", count: 100)).count == 64)
    }
}
