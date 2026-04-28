import XCTest
import WisprAltCore

final class InjectionPredicateTests: XCTestCase {
    // setSucceeded == false: always false regardless of read state
    func test_setFailed_bothReadsOK_differ_returnsFalse() {
        XCTAssertFalse(didInjectionLand(setSucceeded: false, beforeValue: "a", afterValue: "b"))
    }
    func test_setFailed_bothReadsNil_returnsFalse() {
        XCTAssertFalse(didInjectionLand(setSucceeded: false, beforeValue: nil, afterValue: nil))
    }

    // setSucceeded == true, reads succeeded
    func test_setOK_readsOK_valueChanged_returnsTrue() {
        XCTAssertTrue(didInjectionLand(setSucceeded: true, beforeValue: "old", afterValue: "old new"))
    }
    func test_setOK_readsOK_valueUnchanged_nonempty_returnsFalse() {
        XCTAssertFalse(didInjectionLand(setSucceeded: true, beforeValue: "same", afterValue: "same"))
    }
    func test_setOK_readsOK_valueUnchanged_emptyToEmpty_returnsFalse_REGRESSION_PIN() {
        // The historical bug. Old code returned true (false positive in
        // iMessages/Electron). New code: false.
        XCTAssertFalse(didInjectionLand(setSucceeded: true, beforeValue: "", afterValue: ""))
    }
    func test_setOK_readsOK_emptyToText_returnsTrue() {
        // The genuine empty-then-text happy path.
        XCTAssertTrue(didInjectionLand(setSucceeded: true, beforeValue: "", afterValue: "hello"))
    }
    func test_setOK_readsOK_textToEmpty_returnsTrue() {
        // Unusual but possible: caller cleared the field.
        XCTAssertTrue(didInjectionLand(setSucceeded: true, beforeValue: "old", afterValue: ""))
    }

    // setSucceeded == true, read failures
    func test_setOK_beforeReadFailed_afterReadOK_returnsFalse() {
        XCTAssertFalse(didInjectionLand(setSucceeded: true, beforeValue: nil, afterValue: "text"))
    }
    func test_setOK_beforeReadOK_afterReadFailed_returnsFalse() {
        XCTAssertFalse(didInjectionLand(setSucceeded: true, beforeValue: "old", afterValue: nil))
    }
    func test_setOK_bothReadsFailed_returnsFalse() {
        XCTAssertFalse(didInjectionLand(setSucceeded: true, beforeValue: nil, afterValue: nil))
    }

    // Unicode boundary
    func test_setOK_readsOK_unicodeChange_returnsTrue() {
        XCTAssertTrue(didInjectionLand(setSucceeded: true, beforeValue: "café", afterValue: "café 🎉"))
    }
}
