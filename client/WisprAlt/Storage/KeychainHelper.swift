import Foundation
import Security

/// Typed errors for Keychain operations.
enum KeychainError: Error, LocalizedError {
    case unexpectedStatus(OSStatus)
    case encodingFailed
    case itemNotFound

    var errorDescription: String? {
        switch self {
        case .unexpectedStatus(let status):
            let msg = SecCopyErrorMessageString(status, nil) as String? ?? "Unknown error"
            return "Keychain error (\(status)): \(msg)"
        case .encodingFailed:
            return "Failed to encode API key as UTF-8 data."
        case .itemNotFound:
            return "Keychain item not found."
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

    // MARK: - Private helpers

    private static func baseQuery() -> [CFString: Any] {
        [
            kSecClass:       kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account
        ]
    }
}
