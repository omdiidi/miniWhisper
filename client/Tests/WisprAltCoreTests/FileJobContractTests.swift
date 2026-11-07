import Testing
import WisprAltCore

@Suite struct FileJobContractTests {
    /// Must match the server runner's cancel reason byte for byte, or a user
    /// cancel surfaces as a failure toast.
    @Test func cancelledByUserReason_matchesServer() {
        #expect(FileJobContract.cancelledByUserReason == "cancelled by user")
    }
}
