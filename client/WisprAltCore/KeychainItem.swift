import Foundation
import Security

public enum KeychainItemError: LogSafeError, Equatable {
    case unexpectedStatus(OSStatus)
    case encodingFailed

    public var logDescription: String {
        switch self {
        case .unexpectedStatus(let status): return "keychain(status: \(status))"
        case .encodingFailed: return "keychain(encodingFailed)"
        }
    }

    public var outcomeKind: ErrorOutcome { .failed(.other) }
}

/// One generic-password item in the login keychain (legacy file keychain, no
/// access class), addressed by service + account.
public struct KeychainItem: Sendable, Equatable {
    public let service: String
    public let account: String

    public init(service: String, account: String = "default") {
        self.service = service
        self.account = account
    }

    private var query: [CFString: Any] {
        [kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: account]
    }

    /// The stored value, or nil when the item does not exist.
    public func get() throws -> String? {
        var q = query
        q[kSecReturnData] = kCFBooleanTrue
        q[kSecMatchLimit] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data else { return nil }
            return String(data: data, encoding: .utf8)
        case errSecItemNotFound:
            return nil
        default:
            throw KeychainItemError.unexpectedStatus(status)
        }
    }

    /// Updates the item, or adds it when absent.
    public func set(_ value: String) throws {
        guard let data = value.data(using: .utf8) else { throw KeychainItemError.encodingFailed }
        let status = SecItemUpdate(query as CFDictionary, [kSecValueData: data] as CFDictionary)
        switch status {
        case errSecSuccess:
            return
        case errSecItemNotFound:
            var add = query
            add[kSecValueData] = data
            let addStatus = SecItemAdd(add as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw KeychainItemError.unexpectedStatus(addStatus) }
        default:
            throw KeychainItemError.unexpectedStatus(status)
        }
    }

    /// Deletes the item; deleting an absent item is not an error.
    public func delete() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainItemError.unexpectedStatus(status)
        }
    }
}
