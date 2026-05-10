import Foundation

extension Notification.Name {
    /// Posted by `MenuBarController` after a transcript file is successfully written
    /// to disk (meeting or custom). `LastTranscriptCaptionViewModel` instances
    /// observe this and call `refresh()` so their captions update immediately,
    /// independent of the parent-folder DispatchSource watcher (which doesn't
    /// fire for writes inside per-job subfolders).
    static let wisprAltTranscriptWritten = Notification.Name("co.wispralt.transcriptWritten")
}
