import Foundation
import WisprAltCore

/// The estimated OpenAI spend shown in the popover, kept in
/// `~/Library/Application Support/WisprAlt/spend.json` (0600). Providers report
/// each successful request through `record`; nothing leaves the Mac.
@MainActor
final class SpendStore: ObservableObject {
    static let shared = SpendStore()
    private static let url = SpoolFiles.base.appendingPathComponent("spend.json")

    @Published private(set) var ledger: SpendLedger

    private init() {
        ledger = (try? Data(contentsOf: Self.url)).flatMap { try? JSONDecoder().decode(SpendLedger.self, from: $0) }
            ?? SpendLedger()
    }

    /// The `onBilled` hook every OpenAI provider gets.
    nonisolated static func record(_ model: OpenAIModel, seconds: Double) {
        let usd = SpendLedger.cost(model, seconds: seconds)
        Task { @MainActor in shared.add(usd) }
    }

    private func add(_ usd: Decimal) {
        ledger = ledger.adding(usd, at: Date())
        do {
            try PrivateFile.write(try JSONEncoder().encode(ledger), to: Self.url)
        } catch {
            Log.warning("spend save failed: \(ErrorLog.describe(error))", category: "dictation")
        }
    }
}
