import Foundation

/// Which Local server URLs the client accepts (A9): https anywhere, or plain
/// http only to this Mac (`127.0.0.0/8`, `::1`, `localhost`). ATS
/// (`NSAllowsLocalNetworking`) would also admit LAN IPs; this policy is the
/// tighter rule that applies. LAN / tailnet hosts use https (`tailscale serve`).
public enum LocalURLPolicy {
    public static let hint = "Use https://, or http:// only for this Mac"

    public static func isAllowed(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased(), !host.isEmpty else { return false }
        switch url.scheme?.lowercased() {
        case "https": return true
        case "http": return isLoopback(host)
        default: return false
        }
    }

    static func isLoopback(_ host: String) -> Bool {
        let h = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if h == "localhost" || h == "::1" { return true }
        let octets = h.split(separator: ".", omittingEmptySubsequences: false)
        guard octets.count == 4, octets[0] == "127" else { return false }
        return octets.allSatisfy { UInt8($0) != nil }
    }
}
