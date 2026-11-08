import Foundation

/// A portal invite link, `wispralt://join?host=<server URL>&invite=<token>`
/// (server `accounts.service.join_link`), and its redeem call,
/// `POST <host>/invites/redeem` `{invite, device_label?}` → `{token, user_label, host}`.
/// The token and the returned key are never logged.
public struct InviteLink: Equatable, Sendable {
    public static let scheme = "wispralt"
    public static let maxInviteLength = 200   // the server rejects longer

    /// The server to join; always passes `LocalURLPolicy`.
    public let host: URL
    public let invite: String

    /// nil unless `url` is `wispralt://join` with a `host` that passes
    /// `LocalURLPolicy` and a 1...200 character `invite`.
    public static func parse(_ url: URL) -> InviteLink? {
        guard url.scheme?.lowercased() == scheme, url.host?.lowercased() == "join",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let hostText = items.first(where: { $0.name == "host" })?.value,
              let host = URL(string: hostText), LocalURLPolicy.isAllowed(host),
              let invite = items.first(where: { $0.name == "invite" })?.value,
              (1...maxInviteLength).contains(invite.count),
              !invite.contains(where: { $0.isWhitespace || $0.isNewline })
        else { return nil }
        return InviteLink(host: host, invite: invite)
    }

    /// The redeem request. The body is JSON, so the token never lands in a URL.
    public func redeemRequest(deviceLabel: String?) -> URLRequest {
        let url = LocalURLPolicy.url(host, path: "/invites/redeem") ?? host.appendingPathComponent("invites/redeem")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: String] = ["invite": invite]
        if let deviceLabel, !deviceLabel.isEmpty { body["device_label"] = deviceLabel }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        return request
    }

    /// A 200 redeem body. `token` is the new Local API key.
    public struct Redeemed: Decodable, Equatable, Sendable {
        public let token: String
        public let userLabel: String?

        public init(token: String, userLabel: String?) {
            self.token = token
            self.userLabel = userLabel
        }

        enum CodingKeys: String, CodingKey { case token, userLabel = "user_label" }
    }

    public static func decodeRedeem(status: Int, body: Data) throws -> Redeemed {
        switch status {
        case 200:
            guard let r = try? JSONDecoder().decode(Redeemed.self, from: body),
                  !r.token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { throw InviteError.badResponse }
            return r
        case 400: throw InviteError.invalid
        case 429: throw InviteError.rateLimited
        default: throw InviteError.server(status: status)
        }
    }
}

public enum InviteError: LogSafeError, Equatable {
    /// 400 `invite_invalid`: expired, used, revoked or unknown.
    case invalid
    case rateLimited
    case server(status: Int)
    case badResponse

    public var logDescription: String {
        switch self {
        case .invalid: return "invite(invalid)"
        case .rateLimited: return "invite(rateLimited)"
        case .server(let status): return "invite(server \(status))"
        case .badResponse: return "invite(badResponse)"
        }
    }

    public var outcomeKind: ErrorOutcome {
        switch self {
        case .invalid: return .failed(.auth)
        case .rateLimited, .server: return .failed(.server)
        case .badResponse: return .failed(.other)
        }
    }

    /// What the user sees.
    public var message: String {
        switch self {
        case .invalid: return "This invite has expired or was already used. Ask your admin for a new one."
        case .rateLimited: return "Too many attempts. Wait a minute and open the link again."
        case .server, .badResponse: return "The server could not redeem the invite. Try again later."
        }
    }
}
