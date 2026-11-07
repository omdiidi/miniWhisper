/// Pure constants and rules shared by the client's file-job paths.
public enum FileJobContract {
    /// Server-side `failed` reason for a job the user cancelled
    /// (`DELETE /transcribe/file/{id}` → 202, then the runner ends it).
    /// A job that fails with exactly this reason is a silent end, not an error.
    public static let cancelledByUserReason = "cancelled by user"
}
