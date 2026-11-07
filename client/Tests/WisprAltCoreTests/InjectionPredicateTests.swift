import Testing
import WisprAltCore

@Suite struct InjectionPredicateTests {
    // setSucceeded == false: always false regardless of read state
    @Test func setFailed_bothReadsOK_differ_returnsFalse() {
        #expect(!didInjectionLand(setSucceeded: false, beforeValue: "a", afterValue: "b"))
    }
    @Test func setFailed_bothReadsNil_returnsFalse() {
        #expect(!didInjectionLand(setSucceeded: false, beforeValue: nil, afterValue: nil))
    }

    // setSucceeded == true, reads succeeded
    @Test func setOK_readsOK_valueChanged_returnsTrue() {
        #expect(didInjectionLand(setSucceeded: true, beforeValue: "old", afterValue: "old new"))
    }
    @Test func setOK_readsOK_valueUnchanged_nonempty_returnsFalse() {
        #expect(!didInjectionLand(setSucceeded: true, beforeValue: "same", afterValue: "same"))
    }
    @Test func setOK_readsOK_valueUnchanged_emptyToEmpty_returnsFalse_REGRESSION_PIN() {
        // The historical bug. Old code returned true (false positive in
        // iMessages/Electron). New code: false.
        #expect(!didInjectionLand(setSucceeded: true, beforeValue: "", afterValue: ""))
    }
    @Test func setOK_readsOK_emptyToText_returnsTrue() {
        // The genuine empty-then-text happy path.
        #expect(didInjectionLand(setSucceeded: true, beforeValue: "", afterValue: "hello"))
    }
    @Test func setOK_readsOK_textToEmpty_returnsTrue() {
        // Unusual but possible: caller cleared the field.
        #expect(didInjectionLand(setSucceeded: true, beforeValue: "old", afterValue: ""))
    }

    // setSucceeded == true, read failures
    @Test func setOK_beforeReadFailed_afterReadOK_returnsFalse() {
        #expect(!didInjectionLand(setSucceeded: true, beforeValue: nil, afterValue: "text"))
    }
    @Test func setOK_beforeReadOK_afterReadFailed_returnsFalse() {
        #expect(!didInjectionLand(setSucceeded: true, beforeValue: "old", afterValue: nil))
    }
    @Test func setOK_bothReadsFailed_returnsFalse() {
        #expect(!didInjectionLand(setSucceeded: true, beforeValue: nil, afterValue: nil))
    }

    // Unicode boundary
    @Test func setOK_readsOK_unicodeChange_returnsTrue() {
        #expect(didInjectionLand(setSucceeded: true, beforeValue: "café", afterValue: "café 🎉"))
    }
}
