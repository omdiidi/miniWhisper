import Foundation
import WisprAltCore

/// The Local server dictation path. Not a Core `DictationProvider`: Local
/// needs the app-only `LocalCapture` (streaming session + native-rate WAVs).
/// Task 6 binds it to the endpoint captured at FN press; until then the
/// request goes to the configured server URL.
struct LocalServerProvider {
    func finish(capture: LocalCapture) async throws -> DictationResult {
        let text = try await DictationAPI.transcribe(capture: capture)
        return DictationResult(text: text, modelUsed: "local")
    }
}
