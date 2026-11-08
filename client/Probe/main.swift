// WisprAltProbe — real-API assumption gates for Online mode (Part 3, Task 4).
//
//   WisprAltProbe preflight [--only P1|P5]   P5 Keychain read, P1 long-clip report
//   WisprAltProbe suite-file                 every file model on a short clip + one direct m4a upload
//   WisprAltProbe suite-live                 not built yet (Task 11): exits 2
//
// Options: --ledger PATH (spend ledger JSONL), --run ID (groups one run-all.sh
// invocation), --fixture PATH (P1 speech text). run-all.sh passes all three.
// --keychain-service NAME overrides co.wispralt.openai (tests the no-key path).
//
// Exit: 0 all PASS, 1 any FAIL, 2 BLOCKED with no FAIL, 64 usage error.
//
// The OpenAI key is read in-process from the Keychain (co.wispralt.openai /
// default) and goes only into the Authorization header built by
// OpenAIRequests. It is never printed, logged, written or passed in argv.
// Generated audio lives in a temp dir that is removed on exit.
// Never bundled: build-client-local.sh copies only the WisprAlt executable.

import AVFoundation
import Foundation
import Security
import WisprAltCore

// MARK: - Results

enum Status: String { case pass = "PASS", fail = "FAIL", blocked = "BLOCKED" }

var statuses: [Status] = []

func report(_ name: String, _ status: Status, _ detail: String) {
    statuses.append(status)
    print("\(name) \(status.rawValue) \(detail)")
}

func exitCode() -> Int32 {
    if statuses.contains(.fail) { return 1 }
    if statuses.contains(.blocked) { return 2 }
    return 0
}

// MARK: - Arguments

let repoRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

var argv = Array(CommandLine.arguments.dropFirst())

func option(_ name: String) -> String? {
    guard let i = argv.firstIndex(of: name), i + 1 < argv.count else { return nil }
    let value = argv[i + 1]
    argv.removeSubrange(i...(i + 1))
    return value
}

let ledgerURL = URL(fileURLWithPath: option("--ledger")
    ?? repoRoot.appendingPathComponent("tmp/mission/part3/openai-spend.jsonl").path)
let runID = option("--run") ?? "adhoc-\(Int(Date().timeIntervalSince1970))"
let fixtureURL = URL(fileURLWithPath: option("--fixture")
    ?? repoRoot.appendingPathComponent("scripts/online-assumptions/fixtures/speech.txt").path)
let only = option("--only")
/// Test hook: point at an absent service to exercise the no-key BLOCKED path.
let keychainService = option("--keychain-service") ?? "co.wispralt.openai"
let command = argv.first

// MARK: - Spend ledger and caps

/// Every billable call is appended before it is sent; caps are checked first.
/// Pre-flight ≤ $0.30 per run, suites ≤ $1 per run (combined), ledger ≤ $5 ever.
enum SpendScope {
    case preflight, suite

    var cap: Double { self == .preflight ? 0.30 : 1.00 }
    func covers(_ probe: String) -> Bool { self == .preflight ? probe.hasPrefix("P") : probe.hasPrefix("suite-") }
}

enum Ledger {
    static let lifetimeCap = 5.00

    private static func entries() -> [[String: Any]] {
        guard let data = try? Data(contentsOf: ledgerURL), let text = String(data: data, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap {
            (try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any]
        }
    }

    private static func usd(_ e: [String: Any]) -> Double { (e["est_usd"] as? NSNumber)?.doubleValue ?? 0 }

    /// nil when `projected` fits every cap, else the reason it does not.
    static func refusal(projected: Double, scope: SpendScope) -> String? {
        let all = entries()
        let lifetime = all.reduce(0) { $0 + usd($1) }
        let inScope = all.filter { ($0["run"] as? String) == runID && scope.covers($0["probe"] as? String ?? "") }
            .reduce(0) { $0 + usd($1) }
        if lifetime + projected > lifetimeCap {
            return String(format: "spend refused: ledger $%.4f + $%.4f > $%.2f", lifetime, projected, lifetimeCap)
        }
        if inScope + projected > scope.cap {
            return String(format: "spend refused: run $%.4f + $%.4f > $%.2f", inScope, projected, scope.cap)
        }
        return nil
    }

    static func record(probe: String, model: String, seconds: Double, estUSD: Double) {
        let entry: [String: Any] = [
            "ts": ISO8601DateFormatter().string(from: Date()), "run": runID, "probe": probe,
            "model": model, "seconds": NSDecimalNumber(string: String(format: "%.2f", seconds)),
            "est_usd": NSDecimalNumber(string: String(format: "%.6f", estUSD)),
        ]
        guard let line = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return }
        let fm = FileManager.default
        try? fm.createDirectory(at: ledgerURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: ledgerURL.path) { fm.createFile(atPath: ledgerURL.path, contents: nil) }
        guard let handle = try? FileHandle(forWritingTo: ledgerURL) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: line + Data("\n".utf8))
    }

    static func estimate(_ model: OpenAIModel, seconds: Double) -> Double {
        seconds / 60 * NSDecimalNumber(decimal: model.pricePerMinute).doubleValue
    }
}

// MARK: - Key (P5)

enum KeyRead {
    case present(String)
    case blocked(String)
    case failed(String)
}

let keyRead: KeyRead = {
    do {
        guard let key = try KeychainItem(service: keychainService).get(), !key.isEmpty else {
            return .blocked("key missing (\(keychainService))")
        }
        return .present(key)
    } catch KeychainItemError.unexpectedStatus(let status)
        where [errSecAuthFailed, errSecInteractionNotAllowed, errSecUserCanceled].contains(status) {
        return .blocked("keychain read denied (status \(status))")
    } catch {
        return .failed("keychain read: \(ErrorLog.describe(error))")
    }
}()

func requireKey(_ probe: String) -> String? {
    switch keyRead {
    case .present(let key): return key
    case .blocked(let why): report(probe, .blocked, why)
    case .failed(let why): report(probe, .fail, why)
    }
    return nil
}

// MARK: - Audio helpers (temp dir only)

let workDir = FileManager.default.temporaryDirectory.appendingPathComponent("wispralt-probe-\(UUID().uuidString)")
try? FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)

struct ToolFailed: LogSafeError {
    let tool: String
    let code: Int32
    var logDescription: String { "\(tool) exited \(code)" }
    var outcomeKind: ErrorOutcome { .failed(.other) }
}

func run(_ tool: String, _ args: [String]) throws {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: tool)
    p.arguments = args
    try p.run()
    p.waitUntilExit()
    guard p.terminationStatus == 0 else { throw ToolFailed(tool: tool, code: p.terminationStatus) }
}

/// `say -r 212` renders `text` into an AIFF in the temp dir.
func speak(_ text: String, name: String) throws -> URL {
    let txt = workDir.appendingPathComponent("\(name).txt")
    let out = workDir.appendingPathComponent("\(name).aiff")
    try Data(text.utf8).write(to: txt)
    try run("/usr/bin/say", ["-r", "212", "-f", txt.path, "-o", out.path])
    return out
}

func durationSeconds(_ url: URL) throws -> Double {
    let f = try AVAudioFile(forReading: url)
    return Double(f.length) / f.fileFormat.sampleRate
}

func words(_ s: String) -> [String] { s.split(whereSeparator: { $0.isWhitespace }).map(String.init) }

func normalizedWords(_ s: String) -> [String] {
    words(s.lowercased()).map { $0.filter { $0.isLetter || $0.isNumber } }.filter { !$0.isEmpty }
}

// MARK: - HTTP

let session: URLSession = {
    let c = URLSessionConfiguration.ephemeral
    c.timeoutIntervalForRequest = 300
    c.timeoutIntervalForResource = 600
    return URLSession(configuration: c)
}()

struct Reply {
    let transcription: OpenAIRequests.Transcription
    /// `usage` summary without text, e.g. "type=duration seconds=595".
    let usage: String
    let ms: Int
}

/// One request, no retry. Non-2xx → `ProviderError.http` (the body is read only
/// for its error code and is never printed).
func sendOnce(_ request: URLRequest) async throws -> Reply {
    let start = Date()
    let (data, response) = try await session.data(for: request)
    guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
    guard (200..<300).contains(http.statusCode) else {
        throw ProviderError.http(status: http.statusCode, kind: OpenAIErrors.kind(status: http.statusCode, body: data))
    }
    let t = try OpenAIRequests.decodeTranscription(data)
    var usage = "none"
    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let u = obj["usage"] as? [String: Any] {
        usage = u.keys.sorted().compactMap { k in
            if let v = u[k] as? String { return "\(k)=\(v)" }
            if let v = u[k] as? NSNumber { return "\(k)=\(v)" }
            return nil
        }.joined(separator: " ")
    }
    return Reply(transcription: t, usage: usage, ms: Int(Date().timeIntervalSince(start) * 1000))
}

/// Counts the requests the shipped `OpenAIFileProvider` makes (it retries once
/// on a transient error), so every call reaches the ledger.
final class CallCounter: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    var count: Int { lock.lock(); defer { lock.unlock() }; return n }
    func urlSession(_ session: URLSession, didCreateTask task: URLSessionTask) {
        lock.lock(); n += 1; lock.unlock()
    }
}

// MARK: - P1

let p1Window = 590.0...600.0

func runP1() async {
    guard let key = requireKey("P1") else { return }
    let model = OpenAIModels.model(id: "gpt-transcribe")!
    let spoken: String
    do {
        spoken = try String(contentsOf: fixtureURL, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.hasPrefix("#") }.joined(separator: "\n")
    } catch {
        return report("P1", .fail, "probe FAIL (fixture): unreadable fixture")
    }
    let audio: URL, seconds: Double
    do {
        audio = try speak(spoken, name: "p1")
        seconds = try durationSeconds(audio)
    } catch {
        return report("P1", .fail, "probe FAIL (fixture): say/measure failed: \(ErrorLog.describe(error))")
    }
    guard p1Window.contains(seconds) else {
        return report("P1", .fail, String(format: "probe FAIL (fixture): clip is %.1f s, outside 590-600 s; no API call", seconds))
    }
    let est = Ledger.estimate(model, seconds: seconds)
    if let why = Ledger.refusal(projected: est, scope: .preflight) { return report("P1", .blocked, why) }
    let wav: Data
    do { wav = try Mono16kWAV.make(from: audio) } catch {
        return report("P1", .fail, "16 kHz encode failed: \(ErrorLog.describe(error))")
    }
    let request = OpenAIRequests.transcription(model: model, audio: wav, filename: "audio.wav", contentType: "audio/wav",
                                               prompt: nil, responseFormat: "json", key: key)
    Ledger.record(probe: "P1", model: model.id, seconds: seconds, estUSD: est)
    do {
        let r = try await sendOnce(request)
        let spokenCount = words(spoken).count
        let returned = normalizedWords(r.transcription.text)
        let markerOK = returned.suffix(40).contains("zebra")
        let marker = markerOK ? "OK" : "TRUNCATED(measured=\(returned.count))"
        let tokens = r.transcription.outputTokens.map(String.init) ?? "n/a"
        report("P1", .pass, String(format: "report-only: clip=%.1f s wav=%d B spoken_words=%d returned_words=%d "
                                   + "output_tokens=%@ usage{%@} marker=%@ latency_ms=%d est_usd=%.4f",
                                   seconds, wav.count, spokenCount, returned.count, tokens, r.usage, marker, r.ms, est))
        print("P1 tail (fixture text): \(returned.suffix(8).joined(separator: " "))")
    } catch {
        report("P1", .fail, "gpt-transcribe call failed: \(ErrorLog.describe(error))")
    }
}

// MARK: - P5

func runP5() {
    switch keyRead {
    case .present: report("P5", .pass, "keychain read OK: key present (\(keychainService)/default)")
    case .blocked(let why): report("P5", .blocked, why)
    case .failed(let why): report("P5", .fail, why)
    }
}

// MARK: - suite-file

let suiteText = "This is a short test of the file transcription suite. The quick brown fox jumps over the lazy dog."
/// A model the catalog marks UNVERIFIED (A7): one call; model_not_found is a finding, not a failure.
let onceOnlyModel = "gpt-4o-mini-transcribe-2025-03-20"

func heardKeyword(_ text: String) -> Bool { normalizedWords(text).contains("fox") }

func runSuiteFile() async {
    guard let key = requireKey("suite-file") else { return }
    let clip: URL, seconds: Double
    do {
        clip = try speak(suiteText, name: "clip")
        seconds = try durationSeconds(clip)
    } catch {
        return report("suite-file", .fail, "clip generation failed: \(ErrorLog.describe(error))")
    }
    guard seconds > 0, seconds <= 10 else {
        return report("suite-file", .fail, String(format: "clip is %.1f s, must be ≤ 10 s", seconds))
    }
    print(String(format: "suite-file clip: %.1f s", seconds))

    for model in OpenAIModels.all where model.kind == .file {
        let probe = "suite-file/\(model.id)"
        let est = Ledger.estimate(model, seconds: seconds)
        if model.id == onceOnlyModel {
            if let why = Ledger.refusal(projected: est, scope: .suite) { report(probe, .blocked, why); continue }
            let wav: Data
            do { wav = try Mono16kWAV.make(from: clip) } catch {
                report(probe, .fail, "encode: \(ErrorLog.describe(error))"); continue
            }
            Ledger.record(probe: probe, model: model.id, seconds: seconds, estUSD: est)
            do {
                let r = try await sendOnce(OpenAIRequests.transcription(
                    model: model, audio: wav, filename: "audio.wav", contentType: "audio/wav",
                    prompt: nil, responseFormat: "json", key: key))
                let ok = heardKeyword(r.transcription.text)
                report(probe, ok ? .pass : .fail, "1 call, usable (A7 verified) keyword=\(ok) usage{\(r.usage)} latency_ms=\(r.ms)")
            } catch ProviderError.http(let status, .modelNotFound) {
                report(probe, .pass, "1 call, model_not_found (status \(status)): A7 stays UNVERIFIED; the app resets the picker")
            } catch {
                report(probe, .fail, "1 call: \(ErrorLog.describe(error))")
            }
            continue
        }
        // The shipped provider: ≤ 2 requests (one transient retry), so ≤ 3 calls per model.
        if let why = Ledger.refusal(projected: est * 2, scope: .suite) { report(probe, .blocked, why); continue }
        let counter = CallCounter()
        let counted = URLSession(configuration: .ephemeral, delegate: counter, delegateQueue: nil)
        let provider = OpenAIFileProvider(model: model, keyProvider: { key }, session: counted)
        Ledger.record(probe: probe, model: model.id, seconds: seconds, estUSD: est)
        let start = Date()
        do {
            let r = try await provider.finish(Recording(fileURL: clip, durationSeconds: seconds,
                                                        interruptedByDeviceChange: false))
            let ok = heardKeyword(r.text) && r.modelUsed == model.id
            report(probe, ok ? .pass : .fail, "calls=\(counter.count) keyword=\(ok) output_tokens="
                   + "\(r.outputTokens.map(String.init) ?? "n/a") latency_ms=\(Int(Date().timeIntervalSince(start) * 1000))")
        } catch {
            report(probe, .fail, "calls=\(counter.count): \(ErrorLog.describe(error))")
        }
        for _ in 1..<max(1, counter.count) { Ledger.record(probe: probe, model: model.id, seconds: seconds, estUSD: est) }
        counted.finishTasksAndInvalidate()
    }

    // One direct upload of an m4a as is (the Online `.direct` path for a small m4a).
    let probe = "suite-file/m4a-direct"
    let model = OpenAIModels.model(id: OpenAIModels.defaultFileModelID)!
    let est = Ledger.estimate(model, seconds: seconds)
    if let why = Ledger.refusal(projected: est, scope: .suite) { return report(probe, .blocked, why) }
    let m4a = workDir.appendingPathComponent("clip.m4a")
    let bytes: Data
    do {
        try run("/usr/bin/afconvert", ["-f", "m4af", "-d", "aac", clip.path, m4a.path])
        bytes = try Data(contentsOf: m4a)
    } catch {
        return report(probe, .fail, "m4a encode failed: \(ErrorLog.describe(error))")
    }
    Ledger.record(probe: probe, model: model.id, seconds: seconds, estUSD: est)
    do {
        let r = try await sendOnce(OpenAIRequests.transcription(
            model: model, audio: bytes, filename: "clip.m4a", contentType: "audio/mp4",
            prompt: nil, responseFormat: "json", key: key))
        let ok = heardKeyword(r.transcription.text)
        report(probe, ok ? .pass : .fail, "1 call, \(bytes.count) B m4a keyword=\(ok) usage{\(r.usage)} latency_ms=\(r.ms)")
    } catch {
        report(probe, .fail, "1 call: \(ErrorLog.describe(error))")
    }
}

// MARK: - Main

func usage() -> Never {
    FileHandle.standardError.write(Data("usage: WisprAltProbe preflight [--only P1|P5] | suite-file | suite-live\n".utf8))
    try? FileManager.default.removeItem(at: workDir)
    exit(64)
}

switch command {
case "preflight":
    switch only {
    case nil:
        runP5()
        await runP1()
    case "P5": runP5()
    case "P1": await runP1()
    default: usage()
    }
case "suite-file":
    await runSuiteFile()
case "suite-live":
    print("suite-live: not built (Realtime live models arrive in Task 11)")
    try? FileManager.default.removeItem(at: workDir)
    exit(2)
default:
    usage()
}

let code = exitCode()
print("RESULT \(command ?? "") exit=\(code)")
try? FileManager.default.removeItem(at: workDir)
exit(code)
