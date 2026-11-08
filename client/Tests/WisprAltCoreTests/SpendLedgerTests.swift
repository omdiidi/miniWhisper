import Foundation
import Testing
import WisprAltCore

@Suite struct SpendLedgerTests {
    private var cal: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/Phoenix")!
        return c
    }
    private func day(_ y: Int, _ m: Int, _ d: Int, _ h: Int = 12) -> Date {
        cal.date(from: DateComponents(year: y, month: m, day: d, hour: h))!
    }

    @Test func cost_isMinutesTimesPrice() {
        let m = OpenAIModels.model(id: "gpt-transcribe")!       // $0.0045/min
        #expect(SpendLedger.cost(m, seconds: 120) == Decimal(string: "0.009")!)
        #expect(SpendLedger.cost(m, seconds: 0) == 0)
    }

    @Test func todayAndMonth_useLocalDays() {
        let l = SpendLedger()
            .adding(Decimal(string: "0.50")!, at: day(2026, 11, 7), calendar: cal)
            .adding(Decimal(string: "0.25")!, at: day(2026, 11, 8, 1), calendar: cal)
            .adding(Decimal(string: "0.25")!, at: day(2026, 11, 8, 23), calendar: cal)
            .adding(Decimal(string: "9")!, at: day(2026, 10, 31), calendar: cal)
        #expect(l.today(day(2026, 11, 8), calendar: cal) == Decimal(string: "0.5")!)
        #expect(l.thisMonth(day(2026, 11, 8), calendar: cal) == 1)
        #expect(l.today(day(2026, 11, 9), calendar: cal) == 0)
    }

    @Test func oldDaysArePruned_andCodableRoundTrips() throws {
        let l = SpendLedger()
            .adding(1, at: day(2025, 1, 1), calendar: cal)
            .adding(1, at: day(2026, 11, 8), calendar: cal)
        #expect(l.days.count == 1)
        #expect(try JSONDecoder().decode(SpendLedger.self, from: JSONEncoder().encode(l)) == l)
    }

    @Test func format_roundsToCents() {
        #expect(SpendLedger.format(0) == "$0.00")
        #expect(SpendLedger.format(Decimal(string: "0.004")!) == "<$0.01")
        #expect(SpendLedger.format(Decimal(string: "1.236")!) == "$1.24")
    }
}
