import Foundation
import Testing
import WisprAltCore

@Suite struct LocalURLPolicyTests {
    private func allowed(_ s: String) -> Bool { LocalURLPolicy.isAllowed(URL(string: s)!) }

    @Test func https_isAllowedAnywhere() {
        #expect(allowed("https://transcribe.example.com"))
        #expect(allowed("https://mini.tail1234.ts.net:8443"))
    }

    @Test func http_onlyToThisMac() {
        #expect(allowed("http://127.0.0.1:8000"))
        #expect(allowed("http://127.1.2.3"))
        #expect(allowed("http://[::1]:8000"))
        #expect(allowed("http://localhost:8000"))
        #expect(allowed("HTTP://LOCALHOST"))
    }

    @Test func http_toLANOrTailnet_isRejected() {
        #expect(!allowed("http://192.168.1.20:8000"))
        #expect(!allowed("http://10.0.0.5"))
        #expect(!allowed("http://mini.tail1234.ts.net"))
        #expect(!allowed("http://127.0.0.1.evil.com"))
        #expect(!allowed("http://localhost.example.com"))
    }

    @Test func otherSchemes_areRejected() {
        #expect(!allowed("ftp://127.0.0.1"))
        #expect(!allowed("file:///tmp/x"))
        #expect(!allowed("ws://localhost"))
    }

    @Test func url_keepsTheBasePathPrefix() {
        func u(_ base: String, _ path: String) -> String? { LocalURLPolicy.url(URL(string: base)!, path: path)?.absoluteString }
        #expect(u("https://host", "/healthz") == "https://host/healthz")
        #expect(u("https://host/", "/healthz") == "https://host/healthz")
        #expect(u("https://host/wispralt", "/healthz") == "https://host/wispralt/healthz")
        #expect(u("https://host/wispralt/", "/transcribe/file/abc/download/txt") == "https://host/wispralt/transcribe/file/abc/download/txt")
        #expect(u("http://127.0.0.1:8000", "readyz/db") == "http://127.0.0.1:8000/readyz/db")
        #expect(u("https://host/wispralt", "/me/login?next=/me/history") == "https://host/wispralt/me/login?next=/me/history")
        #expect(u("https://host/wispralt?x=1#f", "/me") == "https://host/wispralt/me")
        #expect(LocalURLPolicy.url(URL(string: "file:///tmp")!, path: "/x") == nil)
    }
}
