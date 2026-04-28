import XCTest
import WisprAltCore

final class SecureFieldGateTests: XCTestCase {
    private func ctx(subrole: String, role: String = "AXTextField") -> FocusContext {
        FocusContext(
            bundleID: "com.example", pid: 1234,
            role: role, subrole: subrole
        )
    }

    func test_secureSubrole_refused() {
        XCTAssertTrue(shouldRefuseInjection(for: ctx(subrole: "AXSecureTextField")))
    }
    func test_emptySubrole_allowed() {
        XCTAssertFalse(shouldRefuseInjection(for: ctx(subrole: "")))
    }
    func test_otherSubrole_allowed() {
        XCTAssertFalse(shouldRefuseInjection(for: ctx(subrole: "AXSearchField")))
    }
    func test_secureSubrole_refused_evenIfRoleIsTextArea() {
        XCTAssertTrue(shouldRefuseInjection(for: ctx(subrole: "AXSecureTextField", role: "AXTextArea")))
    }
    /// Pin the invariant: `isSecureField` is now derived from `subrole` so
    /// callers cannot construct an inconsistent context that bypasses the gate.
    func test_isSecureField_derivedFromSubrole() {
        XCTAssertTrue(ctx(subrole: "AXSecureTextField").isSecureField)
        XCTAssertFalse(ctx(subrole: "AXSearchField").isSecureField)
        XCTAssertFalse(ctx(subrole: "").isSecureField)
    }
}
