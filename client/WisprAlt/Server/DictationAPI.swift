import Foundation

/// Namespace for the dictation transcription endpoint.
///
/// Calls `POST /transcribe/dictate` on the configured server URL. If the
/// origin is confirmed offline (per `ServerClient.isOfflineSignature`) and
/// an OpenRouter API key is stored in the Keychain, the request retries
/// directly against OpenRouter's `openai/whisper-large-v3-turbo` model as
/// a one-percent fallback.
///
/// The fallback is intentionally minimal: no Cloudflare Worker, no
/// per-employee rate limiting, no central observability. Each install
/// holds its own OpenRouter key in the Keychain
/// (`co.wispralt.openrouter`); employees with no key set simply see the
/// usual error toast when the mini is unreachable.
enum DictationAPI {
    // MARK: - Response model

    private struct TranscribeResponse: Decodable {
        let text: String
        let model_id: String
        let duration_ms: Double
        let smart_formatted: Bool?
    }

    /// Spacing between origin retry attempts before giving up on the mini.
    private static let retrySpacingSec: TimeInterval = 5

    /// Timeout per OpenRouter request — keeps a hung provider from
    /// dangling the dictation forever.
    private static let openRouterTimeoutSec: TimeInterval = 60

    /// Fallback model — OpenRouter's chat-completions audio path. We tried
    /// `openai/whisper-large-v3-turbo` and `openai/whisper-1` first but both
    /// return 500 Internal Server Error on OpenRouter (verified empirically
    /// 2026-05-09). `gpt-4o-audio-preview` accepts the `input_audio` content
    /// block, returns verbatim transcripts at OpenAI quality, and costs ≈
    /// $0.000025 per typical dictation (negligible for 1% fallback usage).
    private static let fallbackModel = "openai/gpt-4o-audio-preview"

    // MARK: - Public API

    /// Sends PCM WAV data to `POST /transcribe/dictate` and returns the
    /// transcribed text.
    ///
    /// Phase 2 streaming dictation
    /// ---------------------------
    /// When the caller passes a recorder whose `streamingEnabledForThisSession`
    /// snapshot is true AND the recorder reports `streamingArmed == true`
    /// (i.e. at least one chunk has been registered with the actor), this
    /// finalizes the in-flight streaming session by POSTing the tail audio
    /// to `/transcribe/dictate/stream/{sessionId}/finalize` and awaiting all
    /// pending chunk POSTs.
    ///
    /// If the streaming finalize fails for ANY reason (chunk POST error,
    /// finalize 5xx, timeout, decode error, abort) the session is aborted
    /// and we fall through to the existing 3-attempt origin → OpenRouter
    /// ladder, threading the streaming session's `clientDedupId` so the
    /// fallback row collides with any already-persisted streaming row on
    /// the server-side partial unique index — exactly one `dictations` row
    /// per logical dictation regardless of which path won.
    ///
    /// The settings snapshot lives on the recorder (not Settings.shared) so
    /// a mid-recording user toggle cannot orphan a server-side session.
    static func transcribe(
        _ wavData: Data,
        recorder: DictationRecorder? = nil
    ) async throws -> String {
        let streamingEnabled = recorder?.streamingEnabledForThisSession ?? false
        if streamingEnabled,
           let recorder,
           recorder.streamingArmed,
           let session = recorder.streamSession
        {
            let dedupId = session.clientDedupId
            do {
                let tailWav = Int16WAVEncoder.encodeFloat32Frames(
                    recorder.tailFrames(),
                    sampleRate: recorder.inputSampleRate,
                    channels: Int(recorder.inputChannelCount)
                )
                return try await session.finalize(
                    tail: tailWav,
                    smartFormat: Settings.shared.smartFormatting
                )
            } catch {
                Log.warning(
                    "DictationAPI: streaming failed (\(error)); falling back with dedup=\(dedupId).",
                    category: "fallback"
                )
                await session.abort()
                // Thread the same dedup id through the fallback ladder so the
                // ON CONFLICT(client_dedup_id) DO NOTHING index collapses a
                // potential duplicate at server-side ingest.
                return try await runExistingPath(wavData: wavData, clientDedupId: dedupId)
            }
        }
        // Non-streaming path: bit-identical to today (no dedup id header).
        return try await runExistingPath(wavData: wavData, clientDedupId: nil)
    }

    /// VERBATIM port of the pre-Phase-2 transcribe(_:) body — the full
    /// 3-attempt ladder (origin → retry → OpenRouter). The only additive
    /// changes are: (a) thread `clientDedupId` into the `X-Client-Dedup-Id`
    /// header when non-nil; (b) thread the same id into `callOpenRouter`
    /// for the queue-side dedup.
    private static func runExistingPath(
        wavData: Data,
        clientDedupId: String?
    ) async throws -> String {
        let boundary = UUID().uuidString
        let body = buildMultipartBody(wavData: wavData, boundary: boundary)

        var additionalHeaders: [String: String] = [:]
        if Settings.shared.smartFormatting {
            additionalHeaders["X-Smart-Format"] = "true"
        }
        if let id = clientDedupId {
            additionalHeaders["X-Client-Dedup-Id"] = id
        }

        // Attempt 1 — origin.
        let attempt1 = await runOriginAttempt(body: body, boundary: boundary, additionalHeaders: additionalHeaders)
        switch attempt1.result {
        case .success(let text):
            return text
        case .failure(let err, let serverAttempt):
            if !ServerClient.shared.isOfflineSignature(serverAttempt) {
                throw err
            }
            Log.info("DictationAPI: origin attempt 1 → offline signature; retrying once.", category: "fallback")
        }

        // Attempt 2 — origin retry.
        try await Task.sleep(nanoseconds: UInt64(retrySpacingSec * 1_000_000_000))
        let attempt2 = await runOriginAttempt(body: body, boundary: boundary, additionalHeaders: additionalHeaders)
        switch attempt2.result {
        case .success(let text):
            return text
        case .failure(let err, let serverAttempt):
            if !ServerClient.shared.isOfflineSignature(serverAttempt) {
                throw err
            }
            Log.warning("DictationAPI: origin attempt 2 → offline confirmed; trying OpenRouter fallback.", category: "fallback")
        }

        // Attempt 3 — direct OpenRouter call.
        guard let apiKey = try? KeychainHelper.getOpenRouterAPIKey(), !apiKey.isEmpty else {
            Log.warning(
                "DictationAPI: OpenRouter key not configured (set via Settings or `security add-generic-password -s co.wispralt.openrouter`). Cannot fall back.",
                category: "fallback"
            )
            throw ServerError.server(status: 503, body: "Mac mini offline and no OpenRouter fallback key configured")
        }

        return try await callOpenRouter(
            wavData: wavData,
            apiKey: apiKey,
            clientDedupId: clientDedupId
        )
    }

    // MARK: - Origin attempt

    private struct AttemptResult {
        enum Outcome {
            case success(String)
            case failure(Error, ServerClient.RequestAttempt)
        }
        let result: Outcome
    }

    private static func runOriginAttempt(
        body: Data,
        boundary: String,
        additionalHeaders: [String: String]
    ) async -> AttemptResult {
        let startedAt = Date()
        do {
            let request = try ServerClient.shared.buildRequest(
                path: "/transcribe/dictate",
                method: "POST",
                body: body,
                contentType: "multipart/form-data; boundary=\(boundary)",
                additionalHeaders: additionalHeaders
            )
            let (data, http) = try await ServerClient.shared.execute(request, retryOnReset: true)
            let decoded = try JSONDecoder().decode(TranscribeResponse.self, from: data)
            Log.debug(
                "Dictation response: \"\(decoded.text)\" in \(decoded.duration_ms)ms via \(decoded.model_id) (status=\(http.statusCode))",
                category: "dictation"
            )
            return AttemptResult(result: .success(decoded.text))
        } catch {
            let finishedAt = Date()
            let attempt = makeAttempt(error: error, startedAt: startedAt, finishedAt: finishedAt)
            return AttemptResult(result: .failure(mapDecodeError(error), attempt))
        }
    }

    // MARK: - OpenRouter direct call

    /// Calls OpenRouter's `/api/v1/chat/completions` with the audio inlined
    /// as base64 in an `input_audio` content block. Reference:
    /// https://openrouter.ai/docs/guides/overview/multimodal/audio
    ///
    /// Defensive parser: `choices[0].message.content` may come back as a
    /// string OR as a multimodal array `[{type:"text", text:"..."}]`.
    private static func callOpenRouter(
        wavData: Data,
        apiKey: String,
        clientDedupId: String? = nil
    ) async throws -> String {
        let url = URL(string: "https://openrouter.ai/api/v1/chat/completions")!
        // gpt-4o-audio-preview returns clean verbatim text when prompted
        // explicitly. Without the prompt it sometimes adds commentary like
        // "Sure, here's what I heard:" — which would inject into the user's
        // text field. Pin it.
        let payload: [String: Any] = [
            "model": fallbackModel,
            "messages": [[
                "role": "user",
                "content": [
                    [
                        "type": "text",
                        "text": "Transcribe this audio verbatim. Output only the words spoken, no commentary, no preamble, no quotes.",
                    ],
                    [
                        "type": "input_audio",
                        "input_audio": [
                            "data": wavData.base64EncodedString(),
                            "format": "wav",
                        ],
                    ],
                ],
            ]],
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = openRouterTimeoutSec
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("https://github.com/omdiidi/miniWhisper", forHTTPHeaderField: "HTTP-Referer")
        request.setValue("WisprAlt", forHTTPHeaderField: "X-Title")
        request.httpBody = body

        let started = Date()
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw ServerError.transport(error)
        }
        let elapsedMs = Date().timeIntervalSince(started) * 1000
        guard let http = response as? HTTPURLResponse else {
            throw ServerError.transport(URLError(.badServerResponse))
        }
        guard http.statusCode == 200 else {
            let bodyStr = String(data: data, encoding: .utf8) ?? ""
            Log.error(
                "OpenRouter fallback HTTP \(http.statusCode): \(bodyStr.prefix(200))",
                category: "fallback"
            )
            throw ServerError.server(status: http.statusCode, body: bodyStr)
        }

        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let text = parseOpenRouterContent(json)
        Log.info(
            "DictationAPI: source=fallback http=200 chars=\(text.count) ms=\(Int(elapsedMs))",
            category: "fallback"
        )

        // Plan A §A8: persist this cloud-fallback dictation for later sync so it
        // appears in /me/history + period insights. Non-throwing — a queue
        // failure must NEVER break the user's primary "got text" flow.
        let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        DictationFallbackQueue.shared.enqueue(
            text: text,
            dictatedAt: Date(),
            clientAppVersion: appVersion,
            clientDedupId: clientDedupId
        )

        return text
    }

    /// Tolerates both string-shaped and multimodal-array-shaped `content`.
    private static func parseOpenRouterContent(_ json: [String: Any]?) -> String {
        guard let choices = json?["choices"] as? [[String: Any]],
              let message = choices.first?["message"] as? [String: Any]
        else { return "" }
        if let s = message["content"] as? String { return s }
        if let arr = message["content"] as? [[String: Any]] {
            for block in arr {
                if (block["type"] as? String) == "text",
                   let t = block["text"] as? String
                {
                    return t
                }
            }
        }
        return ""
    }

    // MARK: - Classifier helpers

    private static func makeAttempt(
        error: Error,
        startedAt: Date,
        finishedAt: Date
    ) -> ServerClient.RequestAttempt {
        let outcome: ServerClient.RequestAttempt.Outcome
        if let urlErr = error as? URLError {
            outcome = .error(urlErr)
        } else if case ServerError.transport(let underlying) = error {
            outcome = .error(underlying)
        } else if case ServerError.server(let status, _) = error,
                  let synthetic = syntheticResponse(status: status)
        {
            outcome = .response(synthetic)
        } else {
            outcome = .error(error)
        }
        return ServerClient.RequestAttempt(
            startedAt: startedAt,
            finishedAt: finishedAt,
            lastByteSentAt: nil,
            outcome: outcome
        )
    }

    private static func syntheticResponse(status: Int) -> HTTPURLResponse? {
        guard let url = Settings.shared.serverURL else { return nil }
        return HTTPURLResponse(
            url: url,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["X-Request-Id": "synthetic"]
        )
    }

    private static func mapDecodeError(_ error: Error) -> Error {
        if error is DecodingError {
            return ServerError.decoding(error)
        }
        return error
    }

    // MARK: - Multipart body

    private static func buildMultipartBody(wavData: Data, boundary: String) -> Data {
        var body = Data()
        let crlf = "\r\n"
        body.append("--\(boundary)\(crlf)".utf8Data)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\(crlf)".utf8Data)
        body.append("Content-Type: audio/wav\(crlf)".utf8Data)
        body.append(crlf.utf8Data)
        body.append(wavData)
        body.append(crlf.utf8Data)
        body.append("--\(boundary)--\(crlf)".utf8Data)
        return body
    }
}

// MARK: - String → Data helper

private extension String {
    var utf8Data: Data {
        Data(utf8)
    }
}

// MARK: - LastDictation (v0.5.0)

/// Response shape for `GET /me/dictations/last`.
struct LastDictation: Decodable {
    let id: String
    let text: String
    let created_at: Double  // epoch seconds (SQLite REAL → JSON number)
}

/// Namespace for the `/me/dictations/last` endpoint used by the menubar
/// "Copy last dictation" button. Matches the MeAPI.get() pattern exactly:
/// build via `ServerClient.shared.buildRequest`, execute via
/// `ServerClient.shared.execute`, decode with JSONDecoder.
enum LastDictationAPI {
    /// `GET /me/dictations/last` — fetch the caller's most recent
    /// (non-deleted) dictation. Throws `ServerError.server(status: 404, _)`
    /// when the user has zero dictations.
    static func fetch() async throws -> LastDictation {
        let request = try ServerClient.shared.buildRequest(
            path: "/me/dictations/last",
            method: "GET"
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        do {
            return try JSONDecoder().decode(LastDictation.self, from: data)
        } catch {
            throw ServerError.decoding(error)
        }
    }
}
