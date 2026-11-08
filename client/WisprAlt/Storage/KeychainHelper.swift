import Foundation
import os
import WisprAltCore

/// Errors from the Local key export/import file format.
enum KeychainError: Error, LocalizedError {
    case itemNotFound
    case invalidExportFormat

    var errorDescription: String? {
        switch self {
        case .itemNotFound:
            return "Keychain item not found."
        case .invalidExportFormat:
            return "Export file is not a valid WisprAlt key file."
        }
    }
}

/// Façade over the app's three Keychain items (Core `KeychainItem`):
/// the Local server key (`co.wispralt`), the OpenRouter key
/// (`co.wispralt.openrouter`, Mercury) and the OpenAI key (`co.wispralt.openai`).
///
/// Keys are NEVER stored in UserDefaults or any plain-text file.
enum KeychainHelper {
    static let server = KeychainItem(service: "co.wispralt")
    static let openRouter = KeychainItem(service: "co.wispralt.openrouter")
    static let openAI = KeychainItem(service: "co.wispralt.openai")

    // MARK: - Local server key

    static func setAPIKey(_ key: String) throws { try server.set(key) }
    static func getAPIKey() throws -> String? { try server.get() }
    static func deleteAPIKey() throws { try server.delete() }

    // MARK: - OpenRouter key (Mercury smart formatting; uncalled until Mercury moves client-side)

    static func setOpenRouterAPIKey(_ key: String) throws { try openRouter.set(key) }
    static func getOpenRouterAPIKey() throws -> String? { try openRouter.get() }

    // MARK: - Export / Import

    /// Version header written to every exported key file so importAPIKey can
    /// distinguish a genuine export from an arbitrary text file.
    private static let exportFileHeader =
        "# WisprAlt API key export\n# Format: v1\n"

    /// Exports the API key to a plain-text file at `url`.
    ///
    /// The file contains a version header and a single `wispralt_api_key=<KEY>`
    /// line. A best-effort `chmod 0600` is applied; failure is logged but not
    /// fatal (the file still lands on the user's Desktop at mode 0644).
    ///
    /// - Parameter url: Destination URL (typically chosen via NSSavePanel).
    /// - Throws: `KeychainError.itemNotFound` if no API key is stored.
    static func exportAPIKey(to url: URL) throws {
        guard let key = try getAPIKey() else { throw KeychainError.itemNotFound }
        let payload = "\(exportFileHeader)wispralt_api_key=\(key)\n"
        try payload.write(to: url, atomically: true, encoding: .utf8)
        do {
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: url.path)
        } catch {
            Log.warning(
                "Could not set 0600 on exported key file: \(error). File may be world-readable.",
                category: "storage"
            )
        }
    }

    /// Imports an API key from an export file at `url` and saves it to the Keychain.
    ///
    /// The file must contain a `wispralt_api_key=<KEY>` line (comment lines
    /// starting with `#` and blank lines are ignored). Throws
    /// `KeychainError.invalidExportFormat` if the file cannot be parsed or the
    /// key value is empty.
    ///
    /// - Parameter url: Source URL (typically chosen via NSOpenPanel).
    /// - Throws: `KeychainError.invalidExportFormat` on parse failure.
    static func importAPIKey(from url: URL) throws {
        let raw = try String(contentsOf: url, encoding: .utf8)

        // Split on any newline form (CRLF, CR, LF) so files edited on Windows
        // or via webmail don't leave a trailing \r glued to the key — a \r-suffixed
        // key silently 401s every dictation upload because the Bearer header
        // contains it. Codex review caught this.
        let lines = raw.split(whereSeparator: { $0.isNewline })
            .map(String.init)

        let line = lines.first { $0.hasPrefix("wispralt_api_key=") }

        guard let line, let eqIdx = line.firstIndex(of: "=") else {
            throw KeychainError.invalidExportFormat
        }
        // Trim leading/trailing whitespace so a stray space, tab, or trailing
        // carriage return on edited files doesn't poison the keychain.
        let key = line[line.index(after: eqIdx)...]
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { throw KeychainError.invalidExportFormat }

        try setAPIKey(key)
    }
}

/// The OpenAI key, cached in memory. `current()` returns the cache, else
/// re-reads the Keychain, so a key added from Terminal while the app runs is
/// picked up on the next FN press. Warmed at launch; updated on Save/Remove.
/// Safe from any thread.
enum OpenAIKey {
    private static let cache = OSAllocatedUnfairLock<String?>(initialState: nil)

    static func current() -> String? {
        if let key = cache.withLock({ $0 }) { return key }
        let stored: String?
        do {
            stored = try KeychainHelper.openAI.get()
        } catch {
            Log.warning("OpenAI key read failed: \(ErrorLog.describe(error))", category: "keychain")
            return nil
        }
        guard let key = stored?.trimmingCharacters(in: .whitespacesAndNewlines), !key.isEmpty else { return nil }
        cache.withLock { $0 = key }
        return key
    }

    static func save(_ key: String) throws {
        try KeychainHelper.openAI.set(key)
        cache.withLock { $0 = key }
    }

    static func remove() throws {
        try KeychainHelper.openAI.delete()
        cache.withLock { $0 = nil }
    }
}
