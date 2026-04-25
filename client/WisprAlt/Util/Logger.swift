import os

/// Thin wrapper around os.Logger that keeps a consistent subsystem across all log calls.
///
/// Usage:
///     Log.info("Server URL set", category: "settings")
///     Log.error("Keychain write failed: \(err)", category: "keychain")
enum Log {
    private static let subsystem = "co.wispralt"

    // MARK: - Public API

    static func info(_ msg: String, category: String = "general") {
        logger(category: category).info("\(msg, privacy: .public)")
    }

    static func debug(_ msg: String, category: String = "general") {
        logger(category: category).debug("\(msg, privacy: .public)")
    }

    static func error(_ msg: String, category: String = "general") {
        logger(category: category).error("\(msg, privacy: .public)")
    }

    static func warning(_ msg: String, category: String = "general") {
        logger(category: category).warning("\(msg, privacy: .public)")
    }

    // MARK: - Private helpers

    private static func logger(category: String) -> Logger {
        Logger(subsystem: subsystem, category: category)
    }
}
