/// How a Local dictation is sent after FN-up: finalize the streaming session
/// when at least one chunk went out, otherwise upload the whole WAV.
public enum LocalFinalizeRoute: Equatable, Sendable {
    case streamFinalize, wholeFile

    public static func decide(armed: Bool, hasSession: Bool) -> LocalFinalizeRoute {
        armed && hasSession ? .streamFinalize : .wholeFile
    }
}
