import Foundation

/// Estimated OpenAI spend per local calendar day: audio seconds × the model's
/// $/min, recorded after each successful Online request. An estimate: OpenAI's
/// own bill (token-billed models, failed-but-charged calls) is authoritative.
public struct SpendLedger: Codable, Equatable, Sendable {
    /// "yyyy-MM-dd" (local) → USD.
    public private(set) var days: [String: Decimal]
    /// Days kept; older ones are dropped on the next write.
    public static let keepDays = 400

    public init(days: [String: Decimal] = [:]) { self.days = days }

    public static func cost(_ model: OpenAIModel, seconds: Double) -> Decimal {
        guard seconds > 0 else { return 0 }
        return model.pricePerMinute * Decimal(seconds) / 60
    }

    public func adding(_ usd: Decimal, at date: Date, calendar: Calendar = .current) -> SpendLedger {
        guard usd > 0 else { return self }
        var next = days
        next[Self.key(date, calendar), default: 0] += usd
        if let cutoff = calendar.date(byAdding: .day, value: -Self.keepDays, to: date) {
            let oldest = Self.key(cutoff, calendar)
            next = next.filter { $0.key >= oldest }
        }
        return SpendLedger(days: next)
    }

    public func today(_ now: Date, calendar: Calendar = .current) -> Decimal {
        days[Self.key(now, calendar)] ?? 0
    }

    public func thisMonth(_ now: Date, calendar: Calendar = .current) -> Decimal {
        let prefix = String(Self.key(now, calendar).prefix(7))   // "yyyy-MM"
        return days.filter { $0.key.hasPrefix(prefix) }.values.reduce(0, +)
    }

    /// "$0.04", "<$0.01" for a non-zero amount under a cent, "$0.00" for none.
    public static func format(_ usd: Decimal) -> String {
        if usd > 0 && usd < Decimal(string: "0.01")! { return "<$0.01" }
        var value = usd, rounded = Decimal()
        NSDecimalRound(&rounded, &value, 2, .plain)
        return "$" + String(format: "%.2f", NSDecimalNumber(decimal: rounded).doubleValue)
    }

    static func key(_ date: Date, _ calendar: Calendar) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }
}
