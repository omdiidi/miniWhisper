import Foundation
import Testing
import WisprAltCore

@Suite struct OpenAIModelsTests {
    @Test func diarize_isAbsent() {
        #expect(!OpenAIModels.all.contains { $0.id.contains("diarize") })
    }

    @Test func everyDeprecatedModel_hasARetireDate() {
        for m in OpenAIModels.all where m.id != OpenAIModels.defaultFileModelID {
            #expect(m.retires != nil, "\(m.id)")
        }
        #expect(OpenAIModels.model(id: "gpt-4o-mini-transcribe-2025-03-20")?.retires == DateComponents(year: 2027, month: 1, day: 20))
    }

    @Test func exactlyOneFileDefault_gptTranscribe() {
        let fileModels = OpenAIModels.all.filter { $0.kind == .file }
        #expect(fileModels.filter { $0.id == OpenAIModels.defaultFileModelID }.count == 1)
        #expect(OpenAIModels.resolve(id: nil).id == "gpt-transcribe")
        #expect(OpenAIModels.resolve(id: "no-such-model").id == "gpt-transcribe")
        #expect(OpenAIModels.resolve(id: "whisper-1").id == "whisper-1")
        #expect(OpenAIModels.fileModel(for: "unknown").id == "gpt-transcribe")
        #expect(OpenAIModels.fileModel(for: "gpt-4o-transcribe").id == "gpt-4o-transcribe")
    }

    @Test func derivedSafeSeconds_2000tokens_is420() {
        #expect(OpenAIModels.derivedSafeSeconds(2000) == 420)
        #expect(OpenAIModels.dictationCapSeconds(maxOutputTokens: 2000, realtimeCommitLimit: nil) == 420)
        #expect(OpenAIModels.dictationCapSeconds(maxOutputTokens: nil, realtimeCommitLimit: 210) == 210)
    }

    @Test func everyFileModel_capIs600_andFitsTheUploadLimit() {
        for m in OpenAIModels.all where m.kind == .file {
            #expect(m.maxOutputTokens == nil, "\(m.id)")
            #expect(m.dictationCapSeconds == 600, "\(m.id)")
            #expect(m.dictationCapSeconds * 32_000 + 44 < OpenAIModels.uploadLimitBytes, "\(m.id)")
        }
    }

    @Test func displayPrice_andRetireTag_strings() {
        #expect(OpenAIModels.model(id: "gpt-transcribe")?.displayPrice == "$0.0045/min")
        #expect(OpenAIModels.model(id: "gpt-4o-mini-transcribe")?.displayPrice == "≈$0.003/min")
        #expect(OpenAIModels.model(id: "gpt-4o-transcribe")?.displayPrice == "≈$0.006/min")
        #expect(OpenAIModels.model(id: "whisper-1")?.displayPrice == "$0.006/min")
        #expect(OpenAIModels.model(id: "gpt-transcribe")?.retireTag == nil)
        #expect(OpenAIModels.model(id: "whisper-1")?.retireTag == "retires Feb 26, 2027")
        #expect(OpenAIModels.model(id: "gpt-4o-mini-transcribe-2025-03-20")?.retireTag == "retires Jan 20, 2027")
    }

    @Test func modelNotFound_resetsToTheKindDefault() {
        #expect(OpenAIModels.defaultID(for: .file) == "gpt-transcribe")
    }
}
