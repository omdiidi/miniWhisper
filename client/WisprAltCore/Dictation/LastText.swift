import Foundation

/// The one last-dictation record behind "Copy last dictation" (both modes).
/// Saved before any paste, so the text is recoverable whatever delivery does.
/// Written atomically with mode 0600; expires after 7 days. At rest it relies
/// on FileVault.
public struct LastText: Codable, Equatable, Sendable {
    public let text: String
    public let at: Date
    public let mode: TranscriptionMode
    public let modelUsed: String

    public static let maxAge: TimeInterval = 7 * 24 * 3600

    public init(text: String, at: Date, mode: TranscriptionMode, modelUsed: String) {
        self.text = text
        self.at = at
        self.mode = mode
        self.modelUsed = modelUsed
    }

    public func save(to url: URL) throws {
        try PrivateFile.write(try JSONEncoder().encode(self), to: url)
    }

    /// nil when missing, unreadable, or older than 7 days (then deleted).
    public static func load(from url: URL, now: Date = Date()) -> LastText? {
        guard let data = try? Data(contentsOf: url),
              let last = try? JSONDecoder().decode(LastText.self, from: data) else { return nil }
        guard now.timeIntervalSince(last.at) <= maxAge else {
            try? FileManager.default.removeItem(at: url)
            return nil
        }
        return last
    }
}

/// Atomic 0600 writes inside a 0700 folder: the bytes go to a 0600 temp file
/// that is renamed over the target, so no reader ever sees a partial or
/// world-readable file.
public enum PrivateFile {
    public static func write(_ data: Data, to url: URL) throws {
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let tmp = dir.appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        guard FileManager.default.createFile(atPath: tmp.path, contents: data,
                                             attributes: [.posixPermissions: 0o600]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        guard rename(tmp.path, url.path) == 0 else {
            let code = errno
            try? FileManager.default.removeItem(at: tmp)
            throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
        }
    }
}

/// Copies a file over `dest` atomically: a temp copy beside `dest`, then
/// `rename(2)`, so a failed copy never removes an existing `dest`.
public enum AtomicFile {
    public static func copy(_ src: URL, to dest: URL) throws {
        let tmp = dest.deletingLastPathComponent()
            .appendingPathComponent(".\(dest.lastPathComponent).\(UUID().uuidString).tmp")
        try FileManager.default.copyItem(at: src, to: tmp)
        guard rename(tmp.path, dest.path) == 0 else {
            let code = errno
            try? FileManager.default.removeItem(at: tmp)
            throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
        }
    }
}
