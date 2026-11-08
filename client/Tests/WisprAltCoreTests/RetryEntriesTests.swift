import Foundation
import Testing
import WisprAltCore

@Suite struct RetryEntriesTests {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)
    private func e(_ id: String, ago: TimeInterval, secs: Double = 5, audio: Bool = true) -> RetryEntries.Entry {
        .init(id: id, createdAt: now.addingTimeInterval(-ago), durationSeconds: secs, hasAudio: audio)
    }

    @Test func zeroSeconds_deleted() {
        let p = RetryEntries.plan([e("a", ago: 10, secs: 0)], now: now)
        #expect(p.keep.isEmpty && p.delete == ["a"] && p.droppedForSpace == 0)
    }

    @Test func sevenDayBoundary() {
        let week = RetryEntries.maxAge
        let p = RetryEntries.plan([e("in", ago: week), e("out", ago: week + 1)], now: now)
        #expect(p.keep.map(\.id) == ["in"])
        #expect(p.delete == ["out"])
        #expect(p.droppedForSpace == 0)
    }

    @Test func thirdEntry_dropsTheOldest() {
        let p = RetryEntries.plan([e("old", ago: 300), e("new", ago: 10), e("mid", ago: 100)], now: now)
        #expect(p.keep.map(\.id) == ["new", "mid"])
        #expect(p.delete == ["old"])
        #expect(p.droppedForSpace == 1)
    }

    @Test func orphanSidecar_deleted() {
        let p = RetryEntries.plan([e("orphan", ago: 1, audio: false), e("ok", ago: 2)], now: now)
        #expect(p.keep.map(\.id) == ["ok"])
        #expect(p.delete == ["orphan"])
    }

    @Test func actions_bySidecar() throws {
        let online = try SpoolSidecar(mode: .online, model: "gpt-transcribe").encoded()
        let local = try SpoolSidecar(mode: .local, model: "local").encoded()
        #expect(RetryEntries.actions(sidecar: online, currentMode: .online) == ([.retry, .export, .discard], nil))
        #expect(RetryEntries.actions(sidecar: nil, currentMode: .online) == ([.export, .discard], nil))
        #expect(RetryEntries.actions(sidecar: Data("{nope".utf8), currentMode: .local) == ([.export, .discard], nil))
        #expect(RetryEntries.actions(sidecar: local, currentMode: .online) == ([.export, .discard], "Switch to Local to retry"))
        #expect(RetryEntries.actions(sidecar: online, currentMode: .local) == ([.export, .discard], "Switch to Online to retry"))
    }

    @Test func sidecar_roundTrip() throws {
        let s = SpoolSidecar(mode: .local, model: "local")
        #expect(SpoolSidecar.decode(try s.encoded()) == s)
    }
}
