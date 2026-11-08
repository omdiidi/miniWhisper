import Foundation

/// One OpenAI transcription model the picker can offer.
public struct OpenAIModel: Identifiable, Equatable, Sendable {
    public enum Kind: Equatable, Sendable { case file, live }

    public let id: String
    public let kind: Kind
    public let pricePerMinute: Decimal
    /// True when the price is an estimate (token-billed models).
    public let priceIsEstimate: Bool
    public let retires: DateComponents?
    public let supportsPrompt: Bool
    /// Documented output-token ceiling; nil when unverified (no truncation check).
    public var maxOutputTokens: Int?
    /// Largest verified Realtime commit, in seconds (live models only).
    public var realtimeCommitLimit: Int?

    public init(id: String, kind: Kind, pricePerMinute: Decimal, priceIsEstimate: Bool,
                retires: DateComponents?, supportsPrompt: Bool = true,
                maxOutputTokens: Int? = nil, realtimeCommitLimit: Int? = nil) {
        self.id = id
        self.kind = kind
        self.pricePerMinute = pricePerMinute
        self.priceIsEstimate = priceIsEstimate
        self.retires = retires
        self.supportsPrompt = supportsPrompt
        self.maxOutputTokens = maxOutputTokens
        self.realtimeCommitLimit = realtimeCommitLimit
    }

    /// How long a dictation with this model may run before it stops and sends.
    public var dictationCapSeconds: Int {
        OpenAIModels.dictationCapSeconds(maxOutputTokens: maxOutputTokens, realtimeCommitLimit: realtimeCommitLimit)
    }
}

/// The OpenAI transcription catalog. `-diarize` is deliberately absent.
public enum OpenAIModels {
    public static let defaultFileModelID = "gpt-transcribe"
    /// OpenAI's upload limit, taken as decimal megabytes (A11).
    public static let uploadLimitBytes = 25_000_000
    /// Hard ceiling for any dictation, every mode.
    public static let maxDictationSeconds = 600

    private static func date(_ y: Int, _ m: Int, _ d: Int) -> DateComponents {
        DateComponents(year: y, month: m, day: d)
    }

    public static let all: [OpenAIModel] = [
        OpenAIModel(id: "gpt-transcribe", kind: .file, pricePerMinute: Decimal(string: "0.0045")!,
                    priceIsEstimate: false, retires: nil),
        OpenAIModel(id: "gpt-4o-transcribe", kind: .file, pricePerMinute: Decimal(string: "0.006")!,
                    priceIsEstimate: true, retires: date(2027, 2, 26)),
        OpenAIModel(id: "gpt-4o-mini-transcribe", kind: .file, pricePerMinute: Decimal(string: "0.003")!,
                    priceIsEstimate: true, retires: date(2027, 2, 26)),
        OpenAIModel(id: "gpt-4o-mini-transcribe-2025-12-15", kind: .file, pricePerMinute: Decimal(string: "0.003")!,
                    priceIsEstimate: true, retires: date(2027, 2, 26)),
        OpenAIModel(id: "gpt-4o-mini-transcribe-2025-03-20", kind: .file, pricePerMinute: Decimal(string: "0.003")!,
                    priceIsEstimate: true, retires: date(2027, 1, 20)),
        OpenAIModel(id: "whisper-1", kind: .file, pricePerMinute: Decimal(string: "0.006")!,
                    priceIsEstimate: false, retires: date(2027, 2, 26)),
    ]

    public static func model(id: String) -> OpenAIModel? { all.first { $0.id == id } }

    /// The stored model id, or the default when it is unknown or missing.
    public static func resolve(id: String?) -> OpenAIModel {
        id.flatMap(model(id:)) ?? model(id: defaultFileModelID)!
    }

    /// The model used for whole-file uploads chosen from `id`: a file model is
    /// used as is; anything else falls back to the default file model.
    public static func fileModel(for id: String) -> OpenAIModel {
        if let m = model(id: id), m.kind == .file { return m }
        return model(id: defaultFileModelID)!
    }

    /// Seconds of speech that safely fit `maxOutputTokens`: 0.75 words per
    /// token, 170 words per minute, 80 % margin, rounded down to 10 s.
    public static func derivedSafeSeconds(_ maxOutputTokens: Int) -> Int {
        let seconds = Double(maxOutputTokens) * 0.75 / 170 * 60 * 0.8
        return Int((seconds / 10).rounded(.down)) * 10
    }

    public static func dictationCapSeconds(maxOutputTokens: Int?, realtimeCommitLimit: Int?) -> Int {
        min(maxOutputTokens.map(derivedSafeSeconds) ?? maxDictationSeconds,
            realtimeCommitLimit ?? maxDictationSeconds,
            maxDictationSeconds)
    }
}
