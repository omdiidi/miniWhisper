import Testing
import WisprAltCore

@Suite struct LocalFinalizeRouteTests {
    @Test func streamFinalize_onlyWhenArmedWithASession() {
        #expect(LocalFinalizeRoute.decide(armed: true, hasSession: true) == .streamFinalize)
        #expect(LocalFinalizeRoute.decide(armed: false, hasSession: true) == .wholeFile)
        #expect(LocalFinalizeRoute.decide(armed: true, hasSession: false) == .wholeFile)
        #expect(LocalFinalizeRoute.decide(armed: false, hasSession: false) == .wholeFile)
    }
}
