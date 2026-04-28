/// Pure data describing the focused UI element at the moment of an injection
/// attempt. No `AXUIElement` here — this type is intentionally free of any
/// ApplicationServices/AppKit dependency so it can live in the pure-Swift
/// `WisprAltCore` library and be unit-tested without macOS UI plumbing.
public struct FocusContext: Sendable, Equatable {
    public let bundleID: String
    public let pid: Int32
    public let role: String
    public let subrole: String

    public init(bundleID: String, pid: Int32, role: String, subrole: String) {
        self.bundleID = bundleID
        self.pid = pid
        self.role = role
        self.subrole = subrole
    }

    /// Derived from `subrole` so callers cannot construct a `FocusContext`
    /// where `subrole == "AXSecureTextField"` but the secure-field flag is
    /// false (or vice versa) — that would silently bypass the security gate.
    public var isSecureField: Bool {
        subrole == "AXSecureTextField"
    }

    public var description: String {
        "\(bundleID)/pid=\(pid)/role=\(role)/subrole=\(subrole.isEmpty ? "-" : subrole)"
    }
}
