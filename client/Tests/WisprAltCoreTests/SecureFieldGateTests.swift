import Testing
import WisprAltCore

@Suite struct SecureFieldGateTests {
    private func ctx(subrole: String, role: String = "AXTextField") -> FocusContext {
        FocusContext(
            bundleID: "com.example", pid: 1234,
            role: role, subrole: subrole
        )
    }

    @Test func secureSubrole_refused() {
        #expect(shouldRefuseInjection(for: ctx(subrole: "AXSecureTextField")))
    }
    @Test func emptySubrole_allowed() {
        #expect(!shouldRefuseInjection(for: ctx(subrole: "")))
    }
    @Test func otherSubrole_allowed() {
        #expect(!shouldRefuseInjection(for: ctx(subrole: "AXSearchField")))
    }
    @Test func secureSubrole_refused_evenIfRoleIsTextArea() {
        #expect(shouldRefuseInjection(for: ctx(subrole: "AXSecureTextField", role: "AXTextArea")))
    }
    /// Pin the invariant: `isSecureField` is now derived from `subrole` so
    /// callers cannot construct an inconsistent context that bypasses the gate.
    @Test func isSecureField_derivedFromSubrole() {
        #expect(ctx(subrole: "AXSecureTextField").isSecureField)
        #expect(!ctx(subrole: "AXSearchField").isSecureField)
        #expect(!ctx(subrole: "").isSecureField)
    }
}
