import Foundation
import Testing
import WisprAltCore

/// Touches the login keychain, so it runs only where `scripts/test-client.sh`
/// sets `WISPRALT_KEYCHAIN_TESTS=1` (never on CI runners).
@Suite(.enabled(if: ProcessInfo.processInfo.environment["WISPRALT_KEYCHAIN_TESTS"] == "1"))
struct KeychainItemTests {
    @Test func roundTrip_update_delete() throws {
        let item = KeychainItem(service: "co.wispralt.test.\(UUID().uuidString.lowercased())")
        defer { try? item.delete() }
        #expect(try item.get() == nil)
        try item.set("first")
        #expect(try item.get() == "first")
        try item.set("second")
        #expect(try item.get() == "second")
        try item.delete()
        #expect(try item.get() == nil)
        try item.delete()      // deleting an absent item is fine
    }
}
