import Foundation
import Testing
import WisprAltCore

@Suite struct OpenAIRequestsTests {
    private let key = "sk-test-SECRETKEY"
    private let audio = Data("RIFFfakeaudio".utf8)

    private func build(_ model: OpenAIModel = OpenAIModels.resolve(id: nil), prompt: String? = nil) -> URLRequest {
        OpenAIRequests.transcription(model: model, audio: audio, filename: "audio.wav", contentType: "audio/wav",
                                     prompt: prompt, responseFormat: "json", key: key)
    }

    private func bodyText(_ r: URLRequest) -> String { String(decoding: r.httpBody ?? Data(), as: UTF8.self) }

    @Test func bearerHeader_keyNotInURLOrBody() {
        let r = build()
        #expect(r.httpMethod == "POST")
        #expect(r.url == OpenAIRequests.transcriptionsURL)
        #expect(r.value(forHTTPHeaderField: "Authorization") == "Bearer \(key)")
        #expect(!(r.url?.absoluteString.contains(key) ?? true))
        #expect(!bodyText(r).contains(key))
    }

    @Test func multipart_carriesModelFormatAndFile() {
        let r = build()
        let body = bodyText(r)
        #expect(r.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data; boundary=") == true)
        #expect(body.contains("name=\"model\"\r\n\r\ngpt-transcribe\r\n"))
        #expect(body.contains("name=\"response_format\"\r\n\r\njson\r\n"))
        #expect(body.contains("name=\"file\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\nRIFFfakeaudio"))
    }

    @Test func prompt_onlyWhenGivenAndSupported() {
        #expect(!bodyText(build()).contains("name=\"prompt\""))
        #expect(bodyText(build(prompt: "earlier words")).contains("name=\"prompt\"\r\n\r\nearlier words\r\n"))
        let noPrompt = OpenAIModel(id: "x", kind: .file, pricePerMinute: 1, priceIsEstimate: false,
                                   retires: nil, supportsPrompt: false)
        #expect(!bodyText(build(noPrompt, prompt: "earlier words")).contains("name=\"prompt\""))
    }

    @Test func neverSendsLanguage() {
        let body = bodyText(build(prompt: "p"))
        #expect(!body.contains("name=\"language\""))
        #expect(!body.contains("name=\"languages\""))
    }

    @Test func decode_readsTextAndOutputTokens() throws {
        let withUsage = Data(#"{"text":"hello","usage":{"type":"tokens","input_tokens":5,"output_tokens":7}}"#.utf8)
        #expect(try OpenAIRequests.decodeTranscription(withUsage) == .init(text: "hello", outputTokens: 7))
        let durationOnly = Data(#"{"text":"hi","usage":{"type":"duration","seconds":3}}"#.utf8)
        #expect(try OpenAIRequests.decodeTranscription(durationOnly).outputTokens == nil)
        #expect(try OpenAIRequests.decodeTranscription(Data(#"{"text":"x"}"#.utf8)).outputTokens == nil)
    }
}
