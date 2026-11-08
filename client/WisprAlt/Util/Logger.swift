import os

/// Thin wrapper around os.Logger that keeps a consistent subsystem across all log calls.
///
/// Messages are `.public` only in DEBUG builds; release builds log them
/// `.private`, so they read as `<private>` outside the machine's own debugger.
/// Call sites still never log transcript text, response bodies, server
/// reasons, paths, URLs or keys in either build (enforced by
/// scripts/check-log-privacy.py). An `Error` reaches a log only through
/// `ErrorLog.describe`.
///
/// Usage:
///     Log.info("Server URL set", category: "settings")
///     Log.error("Keychain write failed: \(ErrorLog.describe(error))", category: "keychain")
enum Log {
    private static let subsystem = "co.wispralt"

    // MARK: - Public API

    static func info(_ msg: String, category: String = "general") {
        #if DEBUG
        logger(category: category).info("\(msg, privacy: .public)")
        #else
        logger(category: category).info("\(msg, privacy: .private)")
        #endif
    }

    static func debug(_ msg: String, category: String = "general") {
        #if DEBUG
        logger(category: category).debug("\(msg, privacy: .public)")
        #else
        logger(category: category).debug("\(msg, privacy: .private)")
        #endif
    }

    static func error(_ msg: String, category: String = "general") {
        #if DEBUG
        logger(category: category).error("\(msg, privacy: .public)")
        #else
        logger(category: category).error("\(msg, privacy: .private)")
        #endif
    }

    static func warning(_ msg: String, category: String = "general") {
        #if DEBUG
        logger(category: category).warning("\(msg, privacy: .public)")
        #else
        logger(category: category).warning("\(msg, privacy: .private)")
        #endif
    }

    // MARK: - Private helpers

    private static func logger(category: String) -> Logger {
        Logger(subsystem: subsystem, category: category)
    }
}
