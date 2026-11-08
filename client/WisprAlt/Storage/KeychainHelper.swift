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

    // MARK: - OpenRouter key (Mercury smart formatting; read through `OpenRouterKey`)

    static func setOpenRouterAPIKey(_ key: String) throws { try openRouter.set(key) }
    static func getOpenRouterAPIKey() throws -> String? { try openRouter.get() }
}

/// The OpenAI key (`co.wispralt.openai`) and the OpenRouter key
/// (`co.wispralt.openrouter`, Mercury), each cached in memory.
let OpenAIKey = CachedKey(item: KeychainHelper.openAI)
let OpenRouterKey = CachedKey(item: KeychainHelper.openRouter)

/// A Keychain key cached in memory. `current()` returns the cache, else
/// re-reads the Keychain, so a key added from Terminal while the app runs is
/// picked up on the next use. Warmed at launch; updated on Save/Remove.
/// Safe from any thread.
final class CachedKey: @unchecked Sendable {
    private let item: KeychainItem
    private let cache = OSAllocatedUnfairLock<String?>(initialState: nil)

    init(item: KeychainItem) { self.item = item }

    func current() -> String? {
        if let key = cache.withLock({ $0 }) { return key }
        let stored: String?
        do {
            stored = try item.get()
        } catch {
            Log.warning("Keychain key read failed: \(ErrorLog.describe(error))", category: "keychain")
            return nil
        }
        guard let key = stored?.trimmingCharacters(in: .whitespacesAndNewlines), !key.isEmpty else { return nil }
        cache.withLock { $0 = key }
        return key
    }

    func save(_ key: String) throws {
        try item.set(key)
        cache.withLock { $0 = key }
    }

    func remove() throws {
        try item.delete()
        cache.withLock { $0 = nil }
    }
}
