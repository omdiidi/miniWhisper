import Foundation
import WisprAltCore

/// One fresh OpenAI provider per dictation, built from the FN-press snapshot.
/// Local has no provider (`LocalServerProvider` is an app path).
enum ProviderFactory {
    static func make(_ snapshot: DictationSnapshot) -> DictationProvider? {
        switch snapshot.kind {
        case .openAIFile:
            guard let model = snapshot.model else { return nil }
            return OpenAIFileProvider(model: model, keyProvider: { OpenAIKey.current() }, onBilled: SpendStore.record)
        case .openAIRealtime:
            guard let model = snapshot.model else { return nil }
            // A recoverable live failure re-sends the same audio to gpt-transcribe.
            let recovery = OpenAIFileProvider(model: OpenAIModels.fileModel(for: OpenAIModels.defaultFileModelID),
                                              keyProvider: { OpenAIKey.current() }, onBilled: SpendStore.record)
            return OpenAIRealtimeProvider(model: model, keyProvider: { OpenAIKey.current() }, fileRecovery: recovery,
                                          onBilled: SpendStore.record)
        case .local, .needKey:
            return nil
        }
    }
}

/// A dictation in progress: the immutable snapshot taken at FN press and its
/// provider. Main-thread only; held in `MenuBarController.currentSession`
/// from a successful `recorder.start` until FN-up / cap / device change.
struct DictationSession {
    let snapshot: DictationSnapshot
    let provider: DictationProvider?

    /// Where the finished recording goes. `local` is the recorder's Local
    /// output; a Local route without one means no server is configured.
    func route(local: LocalCapture?) throws -> DictationRoute {
        switch snapshot.kind {
        case .openAIFile:
            guard let provider else { throw ProviderError.keyMissing }
            return .openAIFile(provider)
        case .openAIRealtime:
            guard let provider else { throw ProviderError.keyMissing }
            return .openAIRealtime(provider)
        case .local:
            guard let local, let endpoint = snapshot.endpoint else { throw ServerError.missingConfiguration }
            return .local(local, endpoint)
        case .needKey:
            throw ProviderError.keyMissing
        }
    }
}

/// App-only route enum: `LocalCapture` never enters WisprAltCore.
enum DictationRoute {
    case openAIFile(DictationProvider)
    /// Live model: audio already streamed while FN was held; `finish` commits.
    case openAIRealtime(DictationProvider)
    case local(LocalCapture, ServerEndpoint)
}

/// Mercury smart formatting, in both modes, after transcription and before
/// the text is saved or delivered. Runs only when `Settings.smartFormatting`
/// is on; sends text (never audio) to OpenRouter; falls back to the raw text.
enum MercuryStep {
    static func apply(_ result: DictationResult) async -> DictationResult {
        guard Settings.shared.smartFormatting else { return result }
        let (text, formatting) = await MercuryClient(keyProvider: { OpenRouterKey.current() }).format(result.text)
        var out = result
        out.text = text
        out.formatting = formatting
        return out
    }
}
