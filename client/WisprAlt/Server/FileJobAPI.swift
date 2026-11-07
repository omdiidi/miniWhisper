import Foundation
import CryptoKit

// MARK: - Value types

/// Opaque job identifier returned by `POST /transcribe/file` (or chunked finalize).
struct JobID: Hashable, CustomStringConvertible {
    let raw: String
    var description: String { raw }
}

/// Optional per-phase progress block emitted by the server while the job runs.
///
/// All fields are optional — the server only attaches a `progress` block when
/// `status == "running"`, and even then some fields may be absent (e.g.
/// `chunkIndex` is only non-zero during the `transcribe` phase).
struct ProgressInfo: Codable {
    let phase: String?
    let phaseLabel: String?
    let phaseStartedAt: Double?
    let chunkIndex: Int?
    let totalChunks: Int?
    let phaseElapsedS: Double?
    let audioDurationS: Double?

    enum CodingKeys: String, CodingKey {
        case phase
        case phaseLabel = "phase_label"
        case phaseStartedAt = "phase_started_at"
        case chunkIndex = "chunk_index"
        case totalChunks = "total_chunks"
        case phaseElapsedS = "phase_elapsed_s"
        case audioDurationS = "audio_duration_s"
    }
}

/// The state of a server-side transcription job as reported by `GET /transcribe/file/{id}`.
enum JobStatus {
    /// Job created but processing hasn't started yet.
    case pending
    /// Processing is actively running. Carries optional progress block and a
    /// `serverFinishing` flag — true when the user requested cancel but the
    /// executor thread is still running (advisory-cancel semantics).
    case running(ProgressInfo?, Bool)
    /// Processing completed. `outputs` maps format name (e.g. `"json"`, `"srt"`) to a
    /// download URL fragment. Use `FileJobAPI.download(_:format:)` to fetch each file.
    case done(outputs: [String: String])
    /// Processing failed. `error` contains a human-readable reason from the server.
    case failed(error: String)
}

// MARK: - API namespace

/// Namespace for the uploaded-file transcription job endpoints
/// (`/transcribe/file` submit, chunked upload, poll, download, delete/cancel).
enum FileJobAPI {
    // MARK: - Response models

    private struct SubmitResponse: Decodable {
        let job_id: String
        let status: String
    }

    private struct PollResponse: Decodable {
        let status: String
        let outputs: [String: String]?
        let error: String?
        let eta_s: Double?
        let progress: ProgressInfo?
        let serverFinishing: Bool?
    }

    // MARK: - Submit (file — original container, server-side decode)

    /// File-size threshold above which uploads automatically route through
    /// `ChunkedUploader` instead of a single multipart POST. Set just below
    /// Cloudflare's 100 MB request-body limit so the single-shot path is only
    /// used when there's zero risk of an edge rejection. Files at or under
    /// this size keep the simpler code path with no `init`/`finalize`
    /// round-trip overhead.
    static let chunkedUploadThresholdBytes: Int64 = 50 * 1024 * 1024  // 50 MB

    /// Uploads any audio/video container to `POST /transcribe/file`.
    ///
    /// The server runs ffmpeg to transcode the upload to a canonical WAV before
    /// queuing the job. Computes a streaming `Content-MD5` header (no full-file
    /// RAM load) so the server can verify upload integrity, and reports upload
    /// progress via `progress` on the main queue as a fraction in `[0.0, 1.0]`.
    ///
    /// A dedicated `URLSession` with an `UploadSessionDelegate` is created per
    /// upload (300 s inactivity, 6 h wall clock, 6 h request timeout) so slow
    /// uplinks do not trip the default 60 s timers. Uploads are never retried
    /// blindly — the file is too large for a transparent retry.
    ///
    /// - Parameters:
    ///   - sourceURL: Local file URL of the source container (m4a, mp3, mp4, …).
    ///   - progress: Called on the main queue with upload fraction as data is sent.
    /// - Returns: A `JobID` for use with `poll`, `download`, and `delete`.
    /// - Throws: `ServerError` on any HTTP or transport failure, including
    ///   `.fileJobInProgress` (HTTP 429, `code=file_job_busy`) when the server
    ///   is already running a file job.
    static func submitFile(
        _ sourceURL: URL,
        progress: @escaping (Double) -> Void,
        sessionRegistered: (@Sendable (URLSession) -> Void)? = nil
    ) async throws -> JobID {
        // Auto-dispatch to chunked upload for files past Cloudflare's 100 MB
        // body limit, so every caller gets large-file support for free.
        let fileSize = (try? FileManager.default.attributesOfItem(atPath: sourceURL.path)[.size] as? NSNumber)?.int64Value ?? 0
        if fileSize > chunkedUploadThresholdBytes {
            Log.info(
                "submitFile: routing \(fileSize) bytes via chunked upload (>50 MB)",
                category: "transcribe"
            )
            // `submitFile`'s `progress` closure is non-Sendable, but
            // ChunkedUploader's delegate hops to the main actor before
            // calling it — wrap in a sendable shim that re-invokes the
            // caller's closure via a Task on MainActor.
            let progressShim: @Sendable (Double) -> Void = { fraction in
                Task { @MainActor in progress(fraction) }
            }
            return try await ChunkedUploader.upload(
                fileURL: sourceURL,
                progress: progressShim,
                sessionRegistered: sessionRegistered,
                phaseHandler: nil
            )
        }

        guard let baseURL = Settings.shared.serverURL else {
            throw ServerError.missingConfiguration
        }
        guard let url = URL(string: "/transcribe/file", relativeTo: baseURL)?.absoluteURL else {
            throw ServerError.invalidServerURL
        }

        // Content-MD5 over the original container bytes (server compares to the
        // post-multipart-parse file content). Streaming so we never load the
        // entire file into RAM.
        var hasher = Insecure.MD5()
        let hashHandle = try FileHandle(forReadingFrom: sourceURL)
        defer { try? hashHandle.close() }
        while true {
            guard let chunk = try hashHandle.read(upToCount: 1 << 20), !chunk.isEmpty else { break }
            hasher.update(data: chunk)
        }
        let md5Base64 = Data(hasher.finalize()).base64EncodedString()
        try? hashHandle.close()

        // Build the multipart envelope as a temp file so we keep streaming
        // behavior (no full-file RAM load) and can use uploadTask(fromFile:).
        let boundary = "wispralt-" + UUID().uuidString
        let filePartHeader = (
            "--\(boundary)\r\n" +
            "Content-Disposition: form-data; name=\"file\"; filename=\"" +
            sourceURL.lastPathComponent + "\"\r\n" +
            "Content-Type: application/octet-stream\r\n\r\n"
        ).data(using: .utf8)!
        let suffix = "\r\n--\(boundary)--\r\n".data(using: .utf8)!

        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("wispralt-upload-\(UUID().uuidString).tmp")
        FileManager.default.createFile(atPath: tempURL.path, contents: nil)
        do {
            let writer = try FileHandle(forWritingTo: tempURL)
            try writer.write(contentsOf: filePartHeader)
            let reader = try FileHandle(forReadingFrom: sourceURL)
            defer { try? reader.close() }
            while true {
                guard let chunk = try reader.read(upToCount: 1 << 20), !chunk.isEmpty else { break }
                try writer.write(contentsOf: chunk)
            }
            try writer.write(contentsOf: suffix)
            try writer.close()
        } catch {
            try? FileManager.default.removeItem(at: tempURL)
            throw error
        }

        let tempSize = (try FileManager.default.attributesOfItem(atPath: tempURL.path)[.size] as? Int) ?? 0

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue(md5Base64, forHTTPHeaderField: "Content-MD5")
        request.setValue(String(tempSize), forHTTPHeaderField: "Content-Length")
        // Wall-clock cap for the whole upload. Default URLRequest.timeoutInterval
        // is 60 s, which kills any upload from a slow uplink (files can be
        // hundreds of MB at ~15 KB/s home upload = hours).
        request.timeoutInterval = 6 * 60 * 60

        if let apiKey = try? KeychainHelper.getAPIKey() {
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }
        // Phase 1 transcript-storage: see ServerClient.clientVersion.
        request.setValue(
            ServerClient.clientVersion,
            forHTTPHeaderField: "X-WisprAlt-Client-Version"
        )

        let (data, response): (Data, HTTPURLResponse)
        do {
            (data, response) = try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>) in

                let delegate = UploadSessionDelegate(
                    progressHandler: progress,
                    continuation: continuation
                )
                let uploadConfig = URLSessionConfiguration.default
                uploadConfig.timeoutIntervalForRequest = 300       // 5 min inactivity
                uploadConfig.timeoutIntervalForResource = 6 * 60 * 60  // 6 h wall clock
                let uploadSession = URLSession(
                    configuration: uploadConfig,
                    delegate: delegate,
                    delegateQueue: nil
                )
                delegate.session = uploadSession

                // Hand the session up to the caller (MenuBarController) so its
                // `cancelActiveTranscription()` path has a non-nil URLSession to
                // call `invalidateAndCancel()` on. The session is owned by this
                // continuation closure; the delegate's `didCompleteWithError`
                // path will resume the continuation with NSURLErrorCancelled
                // when the caller cancels, so we never leak the continuation.
                sessionRegistered?(uploadSession)

                let task = uploadSession.uploadTask(with: request, fromFile: tempURL)
                task.resume()
            }
        } catch {
            try? FileManager.default.removeItem(at: tempURL)
            throw error
        }
        try? FileManager.default.removeItem(at: tempURL)

        try ServerClient.shared.mapHTTPError(
            status: response.statusCode,
            response: response,
            body: data
        )

        do {
            let decoded = try JSONDecoder().decode(SubmitResponse.self, from: data)
            Log.info("File submitted — job_id: \(decoded.job_id)", category: "transcribe")
            return JobID(raw: decoded.job_id)
        } catch {
            throw ServerError.decoding(error)
        }
    }

    // MARK: - Poll

    /// Polls `GET /transcribe/file/{id}` for the current job status.
    ///
    /// - Parameter id: Job identifier from `submitFile`.
    /// - Returns: A `JobStatus` value representing the current state.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func poll(_ id: JobID) async throws -> JobStatus {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/file/\(id.raw)"
        )
        let (data, _) = try await ServerClient.shared.execute(request)

        do {
            let decoded = try JSONDecoder().decode(PollResponse.self, from: data)
            return mapStatus(decoded)
        } catch {
            throw ServerError.decoding(error)
        }
    }

    // MARK: - Download

    /// Downloads a completed transcript in the given format from
    /// `GET /transcribe/file/{id}/download/{format}`.
    ///
    /// - Parameters:
    ///   - id: Job identifier.
    ///   - format: One of `"json"`, `"srt"`, `"vtt"`, `"txt"`.
    /// - Returns: The raw file bytes.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func download(_ id: JobID, format: String) async throws -> Data {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/file/\(id.raw)/download/\(format)"
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        return data
    }

    // MARK: - Delete

    /// Calls `DELETE /transcribe/file/{id}` to clean up server-side output files
    /// after the client has successfully saved the transcript locally.
    ///
    /// - Parameter id: Job identifier.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func delete(_ id: JobID) async throws {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/file/\(id.raw)",
            method: "DELETE"
        )
        _ = try await ServerClient.shared.execute(request)
        Log.info("Server-side job \(id.raw) deleted.", category: "transcribe")
    }

    // MARK: - Cancel

    /// Request server-side cancellation via `DELETE /transcribe/file/{id}`.
    ///
    /// Server semantics:
    ///   - `202` — the job was pending or running; `cancel_requested` is set
    ///     and the server ends it as `failed: cancelled by user`, discarding
    ///     any outputs. A pending job never starts; a running Whisper call
    ///     keeps the job slot until it returns, then its result is dropped.
    ///   - `204` — the job had already finished (outputs and row removed),
    ///     or it does not exist / is not visible to this key.
    ///
    /// - Parameter id: Job identifier returned by `submitFile` / chunked finalize.
    /// - Returns: The HTTP status code (`202` or `204` on success).
    /// - Throws: `ServerError` on HTTP or transport failure.
    @discardableResult
    static func cancel(_ id: JobID) async throws -> Int {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/file/\(id.raw)",
            method: "DELETE"
        )
        let (_, response) = try await ServerClient.shared.execute(request)
        Log.info("Cancel requested for job \(id.raw) → HTTP \(response.statusCode).", category: "transcribe")
        return response.statusCode
    }

    // MARK: - Server log

    /// Fetch the bracketed server-log slice for a given job via
    /// `GET /admin/server-log/{id}`. Returns the response body as a plain UTF-8
    /// string suitable for rendering in a monospace SwiftUI sheet. Empty
    /// string if the body could not be decoded as UTF-8.
    ///
    /// - Parameter id: Job identifier.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func fetchServerLog(_ id: JobID) async throws -> String {
        let request = try ServerClient.shared.buildRequest(
            path: "/admin/server-log/\(id.raw)"
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        return String(data: data, encoding: .utf8) ?? ""
    }

    // MARK: - Private helpers

    private static func mapStatus(_ response: PollResponse) -> JobStatus {
        switch response.status {
        case "pending":
            return .pending
        case "running":
            return .running(response.progress, response.serverFinishing ?? false)
        case "done":
            return .done(outputs: response.outputs ?? [:])
        case "failed":
            return .failed(error: response.error ?? "Unknown server error")
        default:
            return .failed(error: "Unrecognised status: \(response.status)")
        }
    }
}

// MARK: - Upload session delegate

/// `URLSessionTaskDelegate` + `URLSessionDataDelegate` combination that bridges
/// upload progress and the final response to a Swift concurrency continuation.
///
/// A new instance is created per upload. The `session` property is set after the
/// session is created so the delegate can call `finishTasksAndInvalidate()` on
/// completion, preventing a retain cycle.
private final class UploadSessionDelegate: NSObject,
    URLSessionTaskDelegate,
    URLSessionDataDelegate
{
    private let progressHandler: (Double) -> Void
    private let continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>
    private var accumulator = Data()
    private var httpResponse: HTTPURLResponse?

    /// Weak-ish reference: set after `URLSession` init. We store a strong reference
    /// here (session retains its delegate, delegate retains session) and break the cycle
    /// inside `urlSession(_:task:didCompleteWithError:)` by calling `invalidateAndCancel()`.
    var session: URLSession?

    init(
        progressHandler: @escaping (Double) -> Void,
        continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>
    ) {
        self.progressHandler = progressHandler
        self.continuation = continuation
    }

    // MARK: - URLSessionTaskDelegate

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard totalBytesExpectedToSend > 0 else { return }
        let fraction = Double(totalBytesSent) / Double(totalBytesExpectedToSend)
        DispatchQueue.main.async { [weak self] in
            self?.progressHandler(min(1.0, fraction))
        }
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        // Break the retain cycle as soon as the task is done.
        self.session?.finishTasksAndInvalidate()

        if let error {
            continuation.resume(throwing: ServerError.transport(error))
            return
        }
        guard let http = httpResponse else {
            continuation.resume(throwing: ServerError.transport(URLError(.badServerResponse)))
            return
        }
        continuation.resume(returning: (accumulator, http))
    }

    // MARK: - URLSessionDataDelegate

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        httpResponse = response as? HTTPURLResponse
        completionHandler(.allow)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        accumulator.append(data)
    }
}
