import Foundation
import Testing
import WisprAltCore

@Suite struct InviteLinkTests {
    /// The shape `join_link` produces (urlencoded host, urlsafe token).
    private let link = "wispralt://join?host=https%3A%2F%2Fwispralt.example.com&invite=Ab-_9xYz"

    private func parse(_ s: String) -> InviteLink? { URL(string: s).flatMap(InviteLink.parse) }

    /// Any Local file job (in progress, polled, or saved for resume) refuses the invite.
    @Test func refusedWhileALocalFileJobExists() {
        #expect(!InviteLink.refusedForLocalJob(phase: .none, pollerRunning: false, hasSavedJobID: false))
        for phase in [LocalUploadPhase.converting, .uploading, .polling] {
            #expect(InviteLink.refusedForLocalJob(phase: phase, pollerRunning: false, hasSavedJobID: false))
        }
        #expect(InviteLink.refusedForLocalJob(phase: .none, pollerRunning: true, hasSavedJobID: false))
        #expect(InviteLink.refusedForLocalJob(phase: .none, pollerRunning: false, hasSavedJobID: true))
    }

    @Test func parsesTheServerLink() {
        let l = parse(link)
        #expect(l?.host.absoluteString == "https://wispralt.example.com")
        #expect(l?.invite == "Ab-_9xYz")
        #expect(parse("WISPRALT://JOIN?invite=t&host=http%3A%2F%2F127.0.0.1%3A8000")?.host.absoluteString
                == "http://127.0.0.1:8000")
    }

    @Test func rejectsBadLinks() {
        #expect(parse("https://join?host=https%3A%2F%2Fh.example&invite=t") == nil)          // wrong scheme
        #expect(parse("wispralt://other?host=https%3A%2F%2Fh.example&invite=t") == nil)      // wrong action
        #expect(parse("wispralt://join?invite=t") == nil)                                     // no host
        #expect(parse("wispralt://join?host=https%3A%2F%2Fh.example") == nil)                 // no invite
        #expect(parse("wispralt://join?host=https%3A%2F%2Fh.example&invite=") == nil)
        #expect(parse("wispralt://join?host=https%3A%2F%2Fh.example&invite=\(String(repeating: "a", count: 201))") == nil)
        #expect(parse("wispralt://join?host=https%3A%2F%2Fh.example&invite=a%20b") == nil)
    }

    /// The host must pass `LocalURLPolicy`: no plain http off this Mac, no other schemes.
    @Test func hostMustPassLocalURLPolicy() {
        #expect(parse("wispralt://join?host=http%3A%2F%2F192.168.1.5%3A8000&invite=t") == nil)
        #expect(parse("wispralt://join?host=http%3A%2F%2Fevil.example.com&invite=t") == nil)
        #expect(parse("wispralt://join?host=file%3A%2F%2F%2Ftmp&invite=t") == nil)
        #expect(parse("wispralt://join?host=javascript%3Aalert(1)&invite=t") == nil)
    }

    @Test func redeemRequest_postsJSON_keepsAPathPrefix_tokenNotInURL() throws {
        let l = try #require(parse("wispralt://join?host=https%3A%2F%2Fh.example.com%2Fwispralt&invite=tok123"))
        let r = l.redeemRequest(deviceLabel: "Ana's MacBook")
        #expect(r.httpMethod == "POST")
        #expect(r.url?.absoluteString == "https://h.example.com/wispralt/invites/redeem")
        #expect(r.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(r.value(forHTTPHeaderField: "Authorization") == nil)
        let body = try #require(r.httpBody.flatMap { try JSONSerialization.jsonObject(with: $0) as? [String: String] })
        #expect(body == ["invite": "tok123", "device_label": "Ana's MacBook"])
        #expect(r.url?.absoluteString.contains("tok123") == false)

        let bare = l.redeemRequest(deviceLabel: nil)
        #expect(try JSONSerialization.jsonObject(with: bare.httpBody ?? Data()) as? [String: String] == ["invite": "tok123"])
    }

    @Test func decodeRedeem() throws {
        let ok = Data(#"{"token":"wa_key","user_label":"Ana","host":"https://other.example"}"#.utf8)
        #expect(try InviteLink.decodeRedeem(status: 200, body: ok) == InviteLink.Redeemed(token: "wa_key", userLabel: "Ana"))
        #expect(throws: InviteError.badResponse) { try InviteLink.decodeRedeem(status: 200, body: Data(#"{"token":" "}"#.utf8)) }
        #expect(throws: InviteError.badResponse) { try InviteLink.decodeRedeem(status: 200, body: Data("nope".utf8)) }
        #expect(throws: InviteError.invalid) { try InviteLink.decodeRedeem(status: 400, body: Data(#"{"error":"invite_invalid"}"#.utf8)) }
        #expect(throws: InviteError.rateLimited) { try InviteLink.decodeRedeem(status: 429, body: Data()) }
        #expect(throws: InviteError.server(status: 503)) { try InviteLink.decodeRedeem(status: 503, body: Data()) }
    }

    @Test func errorsAreLogSafe() {
        for e: InviteError in [.invalid, .rateLimited, .server(status: 500), .badResponse] {
            #expect(!ErrorLog.describe(e).contains("tok"))
            #expect(ErrorLog.describe(e).hasPrefix("invite("))
        }
    }
}
