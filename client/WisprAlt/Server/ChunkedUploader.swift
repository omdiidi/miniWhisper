import Foundation

/// Three-step chunked upload client for files >50 MB.
///
/// Why: Cloudflare's free/pro/biz plans enforce a 100 MB request-body limit
/// per request. A 200 MB audio file (or 4 h meeting) would 413 at the edge
/// before reaching the origin. Splitting into 50 MiB chunks keeps every
/// individual request well under the limit; the server reassembles in its
/// staging dir and routes the assembled file through the EXISTING
/// `/transcribe/file` runner path.
///
/// Wire protocol (server: `routes/transcribe_file.py`):
///   1. POST `/transcribe/file/chunked/init`    → `{upload_id, chunk_size}`
///   2. POST `/transcribe/file/chunked/{id}/{i}` raw bytes  → `{ok, received_bytes}`
///   3. POST `/transcribe/file/chunked/{id}/finalize`       → `{job_id, status}`
///
/// One `URLSession` is created at the start of the upload and reused across
/// all three steps (R-K). The `sessionRegistered` callback fires ONCE so
/// `MenuBarController.activeUploadSession` always points at the right session
/// for `cancelActiveTranscription()` to invalidate.
///
/// Progress reporting is per-byte (not per-chunk): `URLSessionTaskDelegate`'s
/// `didSendBodyData` callback feeds the stall-watchdog its
/// `lastUploadProgressAt` heartbeat within a chunk, so a slow 50 MiB chunk on
/// a poor link cannot blow the 120 s stall threshold mid-chunk.
/// Lifecycle phase emitted by `ChunkedUploader.upload` via the optional
/// `phaseHandler` callback. `MenuBarController` toggles
/// `recordingState.isFinalizing` based on `.finalize` so the popover can show
/// an indeterminate "Finalizing" progress bar during server-side chunk concat
/// instead of a misleading "Uploading 99%".
enum ChunkedPhase: Sendable, Equatable {
    case initRequest
    case chunk(index: Int, totalChunks: Int)
    case finalize
}

enum ChunkedUploader {
    /// Hard ceiling — matches server's `_MAX_TOTAL_BYTES` so we 413 client-side
    /// instead of pushing chunks for an upload the server will reject anyway.
    static let maxTotalBytes: Int64 = 4_000_000_000
    /// Default chunk size (50 MiB). Server returns its preferred size in the
    /// `/init` response — if the server's value differs we trust it.
    static let defaultChunkSize = 50 * 1024 * 1024
    /// Threshold above which the caller should pick this path instead of the
    /// single-shot `MeetingAPI.submitFile`.
    static let chunkThreshold: Int64 = 50 * 1024 * 1024

    // MARK: - Wire models

    private struct InitRequest: Encodable {
        let mode: String
        let total_bytes: Int64
        let chunk_count: Int
        let original_filename: String
    }

    private struct InitResponse: Decodable {
        let upload_id: String
        let chunk_size: Int
    }

    private struct FinalizeResponse: Decodable {
        let job_id: String
        let status: String
    }

    // MARK: - Entrypoint

    /// Upload *fileURL* via the chunked path and return the resulting JobID.
    ///
    /// - Parameters:
    ///   - fileURL: Local file to upload.
    ///   - mode: Server-side processing mode (`"file"` or `"meeting"`).
    ///   - progress: Called on the main queue with overall fraction in `[0,1]`.
    ///   - sessionRegistered: Called ONCE with the URLSession used for the
    ///     entire upload so the caller can `invalidateAndCancel()` it on cancel.
    /// - Throws: `ServerError`, `URLError`, or `ChunkedUploaderError` on
    ///   unrecoverable failures.
    static func upload(
        fileURL: URL,
        mode: String,
        progress: @escaping @Sendable (Double) -> Void,
        sessionRegistered: (@Sendable (URLSession) -> Void)? = nil,
        phaseHandler: (@Sendable (ChunkedPhase) -> Void)? = nil
    ) async throws -> JobID {
        guard let baseURL = Settings.shared.serverURL else {
            throw ServerError.missingConfiguration
        }

        // File-size + chunk-count pre-flight.
        let attrs = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        guard let fileSize = (attrs[.size] as? NSNumber)?.int64Value, fileSize > 0 else {
            throw ChunkedUploaderError.invalidFile
        }
        if fileSize > maxTotalBytes {
            throw ChunkedUploaderError.fileTooLarge(fileSize)
        }

        // Build ONE session for the entire upload (R-K). The dedicated
        // delegate ferries per-byte progress out via the `progress` callback.
        let delegate = ChunkedSessionDelegate(progressHandler: progress)
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 300            // 5 min inactivity
        config.timeoutIntervalForResource = 6 * 60 * 60   // 6 h wall clock
        let session = URLSession(
            configuration: config,
            delegate: delegate,
            delegateQueue: nil
        )
        // Register once so the caller can invalidate on cancel. Always
        // invalidate the session before we return — on success via
        // `finishTasksAndInvalidate`, on failure via the `defer` below.
        sessionRegistered?(session)
        var finalized = false
        defer {
            if !finalized {
                session.invalidateAndCancel()
            }
        }

        let apiKey = (try? KeychainHelper.getAPIKey()) ?? ""

        // ── Step 1: /init ──────────────────────────────────────────────────
        phaseHandler?(.initRequest)
        let initResp: InitResponse
        do {
            initResp = try await postJSON(
                session: session,
                url: baseURL.appendingPathComponent("/transcribe/file/chunked/init"),
                apiKey: apiKey,
                body: InitRequest(
                    mode: mode,
                    total_bytes: fileSize,
                    chunk_count: Self.chunkCount(for: fileSize, chunkSize: defaultChunkSize),
                    original_filename: fileURL.lastPathComponent
                ),
                decode: InitResponse.self
            )
        } catch {
            throw ChunkedUploaderError.initFailed(error)
        }
        let uploadID = initResp.upload_id
        let chunkSize = initResp.chunk_size > 0 ? initResp.chunk_size : defaultChunkSize
        let totalChunks = Self.chunkCount(for: fileSize, chunkSize: chunkSize)

        Log.info(
            "ChunkedUploader: init ok — upload_id=\(uploadID) chunks=\(totalChunks) chunk_size=\(chunkSize)",
            category: "transcribe"
        )

        // ── Step 2: chunks ────────────────────────────────────────────────
        let handle = try FileHandle(forReadingFrom: fileURL)
        defer { try? handle.close() }

        for index in 0..<totalChunks {
            try Task.checkCancellation()
            let offset = UInt64(index) * UInt64(chunkSize)
            try handle.seek(toOffset: offset)
            // Last chunk may be smaller — read exactly the remaining bytes.
            let remaining = Int(min(Int64(chunkSize), fileSize - Int64(offset)))
            let chunkData = try handle.read(upToCount: remaining) ?? Data()
            if chunkData.isEmpty {
                throw ChunkedUploaderError.invalidFile
            }

            // R-K + watchdog: tell the delegate which chunk we're on so per-
            // byte fractions can be folded into an overall progress value.
            delegate.beginChunk(index: index, totalChunks: totalChunks, chunkBytes: chunkData.count)
            phaseHandler?(.chunk(index: index, totalChunks: totalChunks))

            Log.info(
                "chunk \(index + 1)/\(totalChunks) starting (size=\(chunkData.count) B)",
                category: "transcribe"
            )
            let chunkStartedAt = Date()
            do {
                try await uploadOneChunk(
                    session: session,
                    baseURL: baseURL,
                    apiKey: apiKey,
                    uploadID: uploadID,
                    chunkIndex: index,
                    data: chunkData
                )
            } catch let urlError as URLError where urlError.code == .cancelled {
                // Caller-initiated cancel — propagate without wrapping.
                throw urlError
            } catch {
                throw ChunkedUploaderError.chunkUploadFailed(error)
            }
            let elapsedMs = Int(Date().timeIntervalSince(chunkStartedAt) * 1000)
            Log.info(
                "chunk \(index + 1)/\(totalChunks) uploaded in \(elapsedMs)ms",
                category: "transcribe"
            )
        }

        // ── Step 3: /finalize ─────────────────────────────────────────────
        phaseHandler?(.finalize)
        let finalResp: FinalizeResponse
        do {
            finalResp = try await postJSON(
                session: session,
                url: baseURL.appendingPathComponent("/transcribe/file/chunked/\(uploadID)/finalize"),
                apiKey: apiKey,
                body: EmptyBody(),
                decode: FinalizeResponse.self
            )
        } catch {
            throw ChunkedUploaderError.finalizeFailed(error)
        }

        Log.info("ChunkedUploader: finalize ok — job_id=\(finalResp.job_id)", category: "transcribe")
        session.finishTasksAndInvalidate()
        finalized = true
        // Ensure progress hits 100% (the per-chunk fractional logic may stop
        // short of exactly 1.0 due to integer rounding on a tiny final chunk).
        await MainActor.run { progress(1.0) }
        return JobID(raw: finalResp.job_id)
    }

    // MARK: - Per-chunk upload with single retry

    /// Upload one chunk via `uploadTask(with:from:)`. Retries once on a
    /// transient `URLError` (network reset, dropped connection). Beyond that,
    /// propagate so the caller can cancel + clean up.
    private static func uploadOneChunk(
        session: URLSession,
        baseURL: URL,
        apiKey: String,
        uploadID: String,
        chunkIndex: Int,
        data: Data
    ) async throws {
        let url = baseURL.appendingPathComponent(
            "/transcribe/file/chunked/\(uploadID)/\(chunkIndex)"
        )
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(String(data.count), forHTTPHeaderField: "Content-Length")
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 6 * 60 * 60

        var lastError: Error?
        for attempt in 0..<2 {
            try Task.checkCancellation()
            do {
                let (body, response) = try await uploadDataAwaiting(
                    session: session,
                    request: request,
                    data: data
                )
                try ServerClient.shared.mapHTTPError(
                    status: response.statusCode,
                    response: response,
                    body: body
                )
                return
            } catch let urlError as URLError {
                // Transient network errors → retry once. Cancelled = propagate.
                if urlError.code == .cancelled {
                    throw urlError
                }
                lastError = urlError
                Log.warning(
                    "ChunkedUploader: chunk \(chunkIndex) failed (\(urlError.code.rawValue)), retry=\(attempt + 1)/2",
                    category: "transcribe"
                )
                continue
            } catch ServerError.transport(let underlying) {
                if let urlError = underlying as? URLError, urlError.code == .cancelled {
                    throw urlError
                }
                lastError = underlying
                Log.warning(
                    "ChunkedUploader: chunk \(chunkIndex) transport error, retry=\(attempt + 1)/2",
                    category: "transcribe"
                )
                continue
            }
        }
        throw lastError ?? ServerError.transport(URLError(.unknown))
    }

    // MARK: - Plumbing

    /// Compute how many chunks an upload of *fileSize* bytes will be split
    /// into given the agreed-on *chunkSize* (ceil division).
    private static func chunkCount(for fileSize: Int64, chunkSize: Int) -> Int {
        let cs = Int64(chunkSize)
        return Int((fileSize + cs - 1) / cs)
    }

    /// POST a JSON-encoded body and decode the JSON response.
    private static func postJSON<Body: Encodable, Resp: Decodable>(
        session: URLSession,
        url: URL,
        apiKey: String,
        body: Body,
        decode: Resp.Type
    ) async throws -> Resp {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 60

        let encoded = try JSONEncoder().encode(body)
        let (data, response) = try await uploadDataAwaiting(
            session: session,
            request: request,
            data: encoded
        )
        try ServerClient.shared.mapHTTPError(
            status: response.statusCode,
            response: response,
            body: data
        )
        do {
            return try JSONDecoder().decode(Resp.self, from: data)
        } catch {
            throw ServerError.decoding(error)
        }
    }

    /// Wrap `URLSession.uploadTask(with:from:)` in an async/await call that
    /// uses the session's delegate (so progress callbacks flow).
    private static func uploadDataAwaiting(
        session: URLSession,
        request: URLRequest,
        data: Data
    ) async throws -> (Data, HTTPURLResponse) {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<(Data, HTTPURLResponse), Error>) in
                let task = session.uploadTask(with: request, from: data) { respData, response, error in
                    if let error {
                        cont.resume(throwing: ServerError.transport(error))
                        return
                    }
                    guard let http = response as? HTTPURLResponse else {
                        cont.resume(throwing: ServerError.transport(URLError(.badServerResponse)))
                        return
                    }
                    cont.resume(returning: (respData ?? Data(), http))
                }
                task.resume()
            }
        } onCancel: {
            // The session-level `invalidateAndCancel` in the caller's `defer`
            // will tear down any in-flight task; explicit task cancel here is
            // a no-op when the parent Task is the one being cancelled.
        }
    }

    private struct EmptyBody: Encodable {}
}

// MARK: - Errors

enum ChunkedUploaderError: Error, LocalizedError {
    case invalidFile
    case fileTooLarge(Int64)
    case initFailed(Error)
    case chunkUploadFailed(Error)
    case finalizeFailed(Error)

    var errorDescription: String? {
        switch self {
        case .invalidFile:
            return "Cannot read file for chunked upload."
        case .fileTooLarge(let bytes):
            let gb = Double(bytes) / 1_000_000_000.0
            return String(format: "File is %.2f GB — exceeds 4 GB chunked-upload limit.", gb)
        case .initFailed(let underlying):
            return "Couldn't start upload: \(underlying.localizedDescription)"
        case .chunkUploadFailed(let underlying):
            return "Upload failed: \(underlying.localizedDescription)"
        case .finalizeFailed(let underlying):
            return "Upload assembled but server rejected: \(underlying.localizedDescription)"
        }
    }
}

// MARK: - Session delegate

/// Per-upload `URLSessionTaskDelegate` that translates per-task `didSendBodyData`
/// into an overall `[0,1]` progress fraction across the whole chunked upload.
///
/// The delegate is given the per-chunk byte budget via `beginChunk(index:totalChunks:chunkBytes:)`
/// before each chunk's upload task starts. Within a chunk, progress is
///   `(chunksDone * chunkBytes + currentBytesSent) / totalEstimatedBytes`,
/// which is good enough for the `ProgressView` and crucially keeps
/// `lastUploadProgressAt` ticking within a chunk (R-K + watchdog R2 #16).
private final class ChunkedSessionDelegate: NSObject, URLSessionTaskDelegate {
    private let progressHandler: @Sendable (Double) -> Void

    // Mutated only by the URLSession's delegate queue + the main thread (via
    // `beginChunk`). Both writes occur strictly serially with respect to the
    // upload task lifecycle so a plain lock-free assignment is safe here.
    private var currentChunkIndex: Int = 0
    private var totalChunks: Int = 1
    private var currentChunkBytes: Int = 1

    init(progressHandler: @escaping @Sendable (Double) -> Void) {
        self.progressHandler = progressHandler
    }

    func beginChunk(index: Int, totalChunks: Int, chunkBytes: Int) {
        self.currentChunkIndex = index
        self.totalChunks = max(1, totalChunks)
        self.currentChunkBytes = max(1, chunkBytes)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        let chunkFraction: Double
        if totalBytesExpectedToSend > 0 {
            chunkFraction = min(1.0, Double(totalBytesSent) / Double(totalBytesExpectedToSend))
        } else {
            chunkFraction = 0
        }
        let overall = (Double(currentChunkIndex) + chunkFraction) / Double(totalChunks)
        let clamped = min(1.0, max(0.0, overall))
        let handler = progressHandler
        DispatchQueue.main.async {
            handler(clamped)
        }
    }
}
