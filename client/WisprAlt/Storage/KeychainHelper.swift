import Foundation
import os
import WisprAltCore

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
