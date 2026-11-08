import Foundation
import WisprAltCore

/// One fresh OpenAI provider per dictation, built from the FN-press snapshot.
/// Local has no provider (`LocalServerProvider` is an app path).
enum ProviderFactory {
    static func make(_ snapshot: DictationSnapshot) -> DictationProvider? {
        switch snapshot.kind {
        case .openAIFile:
            guard let model = snapshot.model else { return nil }
            return OpenAIFileProvider(model: model, keyProvider: { OpenAIKey.current() })
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
    case local(LocalCapture, ServerEndpoint)
}
