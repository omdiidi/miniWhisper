/// Pure: should we refuse to inject into the focused element because it is
/// a secure (password) field?
///
/// True when `context.isSecureField` is set. Native AppKit `NSSecureTextField`
/// and SwiftUI `SecureField` reliably surface `kAXSubroleAttribute ==
/// AXSecureTextField`; web password inputs (Safari/Chrome/Electron) usually
/// do not, so this gate cannot guarantee universal password protection. See
/// `docs/TROUBLESHOOTING.md` for the documented limitation.
public func shouldRefuseInjection(for context: FocusContext) -> Bool {
    return context.isSecureField
}
