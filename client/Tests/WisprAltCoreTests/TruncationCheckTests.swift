import Testing
import WisprAltCore

@Suite struct TruncationCheckTests {
    @Test func table() {
        #expect(!TruncationCheck.isTruncated(outputTokens: nil, verifiedCap: 2000))
        #expect(!TruncationCheck.isTruncated(outputTokens: 1999, verifiedCap: 2000))
        #expect(TruncationCheck.isTruncated(outputTokens: 2000, verifiedCap: 2000))
        #expect(!TruncationCheck.isTruncated(outputTokens: 5000, verifiedCap: nil))
    }
}
