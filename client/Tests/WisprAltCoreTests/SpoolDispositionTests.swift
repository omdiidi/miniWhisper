import Testing
import WisprAltCore

@Suite struct SpoolDispositionTests {
    private let all: [InjectOutcome?] = [.insertedVerified, .pastedUnverified, .refusedSecureField, .noTarget]
    private func d(_ o: DictationOutcome, _ i: InjectOutcome?) -> SpoolDecision { SpoolDisposition.decide(o, delivery: i) }

    @Test func text_delivered_deletes() {
        #expect(d(.text(truncated: false, serverSaved: true), .insertedVerified) == SpoolDecision(keep: false))
        #expect(d(.text(truncated: false, serverSaved: true), .pastedUnverified) == SpoolDecision(keep: false))
        #expect(d(.text(truncated: false, serverSaved: true), .refusedSecureField) == SpoolDecision(keep: false))
        #expect(d(.text(truncated: false, serverSaved: true), .noTarget) == SpoolDecision(keep: false, toasts: [.copiedNotPasted]))
    }

    @Test func truncated_keeps() {
        #expect(d(.text(truncated: true, serverSaved: true), .insertedVerified) == SpoolDecision(keep: true, toasts: [.possiblyTruncated]))
        #expect(d(.text(truncated: true, serverSaved: true), .refusedSecureField) == SpoolDecision(keep: true, toasts: [.possiblyTruncated]))
        #expect(d(.text(truncated: true, serverSaved: true), .noTarget)
                == SpoolDecision(keep: true, toasts: [.possiblyTruncated, .copiedNotPasted]))
    }

    /// Part 4 contract: a 200 with `saved:false` delivers the text and keeps the audio + Retry entry.
    @Test func localSavedFalse_keeps_forEveryDelivery() {
        for i in all.compactMap({ $0 }) {
            let out = d(.text(truncated: false, serverSaved: false), i)
            #expect(out.keep)
            #expect(out.toasts.first == .notSavedOnServer)
        }
    }

    @Test func text_withoutDelivery_keeps() {
        #expect(d(.text(truncated: false, serverSaved: true), nil) == SpoolDecision(keep: true))
    }

    @Test func empty_deletesSilently() {
        for i in all + [nil] {
            #expect(d(.emptyText, i) == SpoolDecision(keep: false))
            #expect(d(.emptyRecording, i) == SpoolDecision(keep: false))
        }
        #expect(DictationOutcome(.empty) == .emptyRecording)
    }

    @Test func failures_keep() {
        for k: FailureKind in [.keyMissing, .auth, .quota, .modelNotFound, .network, .server, .deadline,
                               .deviceChanged, .writeFailed, .encodeFailed, .other] {
            #expect(d(.failed(k), nil) == SpoolDecision(keep: true, toasts: [.failed(k)]))
        }
    }

    @Test func cancelled_isOther_andKept() {
        #expect(DictationOutcome(.cancelled) == .failed(.other))
        #expect(d(DictationOutcome(.cancelled), nil).keep)
    }

    /// Part 4 contract: 410 dictation_deleted / 409 dedup_conflict are terminal — audio discarded.
    @Test func rejected_isTerminal_deletes() {
        #expect(DictationOutcome(ErrorLog.outcome(LocalDictationError.dictationDeleted)) == .failed(.rejected))
        #expect(DictationOutcome(ErrorLog.outcome(LocalDictationError.dedupConflict)) == .failed(.rejected))
        #expect(d(.failed(.rejected), nil) == SpoolDecision(keep: false, toasts: [.failed(.rejected)]))
    }
}
