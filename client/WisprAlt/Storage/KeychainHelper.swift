import Foundation
import Security

/// Typed errors for Keychain operations.
enum KeychainError: Error, LocalizedError {
    case unexpectedStatus(OSStatus)
    case encodingFailed
    case itemNotFound
    case invalidExportFormat

    var errorDescription: String? {
        switch self {
        case .unexpectedStatus(let status):
            let msg = SecCopyErrorMessageString(status, nil) as String? ?? "Unknown error"
            return "Keychain error (\(status)): \(msg)"
        case .encodingFailed:
            return "Failed to encode API key as UTF-8 data."
        case .itemNotFound:
            return "Keychain item not found."
        case .invalidExportFormat:
            return "Export file is not a valid WisprAlt key file."
        }
    }
}

/// Manages the WisprAlt API key in the macOS Keychain.
///
/// The API key is NEVER stored in UserDefaults or any plain-text file.
/// All operations use kSecClassGenericPassword with a stable service identifier.
enum KeychainHelper {
    // MARK: - Constants

    static let service = "co.wispralt"
    private static let account = "default"

    /// Service identifier for the OpenRouter API key used by the cloud
    /// fallback path (`DictationAPI` calls when the Mac mini origin is
    /// unreachable). Stored in a separate Keychain item so rotating it
    /// doesn't touch the WisprAlt bearer.
    private static let openRouterService = "co.wispralt.openrouter"

    // MARK: - Public API

    /// Stores (or updates) the API key in the Keychain.
    static func setAPIKey(_ key: String) throws {
        guard let data = key.data(using: .utf8) else {
            throw KeychainError.encodingFailed
        }

        // Try to update an existing item first.
        let query = baseQuery()
        let attributes: [CFString: Any] = [
            kSecValueData: data
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)

        switch updateStatus {
        case errSecSuccess:
            Log.debug("API key updated in Keychain.", category: "keychain")
        case errSecItemNotFound:
            // Item doesn't exist yet; add it.
            var addQuery = baseQuery() as [CFString: Any]
            addQuery[kSecValueData] = data
            let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw KeychainError.unexpectedStatus(addStatus)
            }
            Log.debug("API key added to Keychain.", category: "keychain")
        default:
            throw KeychainError.unexpectedStatus(updateStatus)
        }
    }

    /// Retrieves the API key from the Keychain. Returns nil if not set.
    static func getAPIKey() throws -> String? {
        var query = baseQuery() as [CFString: Any]
        query[kSecReturnData] = kCFBooleanTrue
        query[kSecMatchLimit] = kSecMatchLimitOne

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)

        switch status {
        case errSecSuccess:
            guard let data = result as? Data,
                  let key = String(data: data, encoding: .utf8)
            else {
                Log.error("Keychain returned data that could not be decoded as UTF-8.", category: "keychain")
                return nil
            }
            return key
        case errSecItemNotFound:
            return nil
        default:
            throw KeychainError.unexpectedStatus(status)
        }
    }

    /// Deletes the stored API key from the Keychain (e.g. on uninstall or key rotation).
    static func deleteAPIKey() throws {
        let query = baseQuery()
        let status = SecItemDelete(query as CFDictionary)
        switch status {
        case errSecSuccess, errSecItemNotFound:
            Log.debug("API key deleted from Keychain (or was already absent).", category: "keychain")
        default:
            throw KeychainError.unexpectedStatus(status)
        }
    }

    // MARK: - OpenRouter API key (cloud fallback)

    /// Stores (or updates) the OpenRouter API key used by the cloud fallback
    /// path. Optional — when absent, dictation simply errors out when the
    /// mini is offline instead of falling back.
    static func setOpenRouterAPIKey(_ key: String) throws {
        guard let data = key.data(using: .utf8) else { throw KeychainError.encodingFailed }

        let query = openRouterBaseQuery()
        let attributes: [CFString: Any] = [kSecValueData: data]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)

        switch updateStatus {
        case errSecSuccess:
            Log.debug("OpenRouter API key updated in Keychain.", category: "keychain")
        case errSecItemNotFound:
            var addQuery = openRouterBaseQuery() as [CFString: Any]
            addQuery[kSecValueData] = data
            let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw KeychainError.unexpectedStatus(addStatus) }
            Log.debug("OpenRouter API key added to Keychain.", category: "keychain")
        default:
            throw KeychainError.unexpectedStatus(updateStatus)
        }
    }

    /// Retrieves the OpenRouter API key. Returns nil when not set, which
    /// disables the cloud fallback path.
    static func getOpenRouterAPIKey() throws -> String? {
        var query = openRouterBaseQuery() as [CFString: Any]
        query[kSecReturnData] = kCFBooleanTrue
        query[kSecMatchLimit] = kSecMatchLimitOne

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)

        switch status {
        case errSecSuccess:
            guard let data = result as? Data,
                  let key = String(data: data, encoding: .utf8)
            else { return nil }
            return key
        case errSecItemNotFound:
            return nil
        default:
            throw KeychainError.unexpectedStatus(status)
        }
    }

    private static func openRouterBaseQuery() -> [CFString: Any] {
        [
            kSecClass:       kSecClassGenericPassword,
            kSecAttrService: openRouterService,
            kSecAttrAccount: account,
        ]
    }

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

    // MARK: - Private helpers

    private static func baseQuery() -> [CFString: Any] {
        [
            kSecClass:       kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account
        ]
    }
}
