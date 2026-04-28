/// Pure: was the AX injection observably successful?
///
/// Returns true iff `setSucceeded` AND both reads succeeded AND value changed.
/// Empty-before/empty-after with set-success returns FALSE — that combination
/// is the signature of an AX layer that silently no-ops the write (Electron
/// contenteditable, custom NSTextView like iMessages compose).
public func didInjectionLand(
    setSucceeded: Bool,
    beforeValue: String?,
    afterValue: String?
) -> Bool {
    guard setSucceeded else { return false }
    guard let before = beforeValue, let after = afterValue else { return false }
    return after != before
}
