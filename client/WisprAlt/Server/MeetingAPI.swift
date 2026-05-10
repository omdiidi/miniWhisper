import Foundation
import CryptoKit

// MARK: - Value types

/// Opaque job identifier returned by `POST /transcribe/meeting`.
struct JobID: Hashable, CustomStringConvertible {
    let raw: String
    var description: String { raw }
}

/// The state of a server-side transcription job as reported by `GET /transcribe/meeting/{id}`.
enum JobStatus {
    /// Job created but processing hasn't started yet.
    case pending
    /// Processing is actively running.
    case running
    /// Processing completed. `outputs` maps format name (e.g. `"json"`, `"srt"`) to a
    /// download URL fragment. Use `MeetingAPI.download(_:format:)` to fetch each file.
    case done(outputs: [String: String])
    /// Processing failed. `error` contains a human-readable reason from the server.
    case failed(error: String)
}

// MARK: - API namespace

/// Namespace for the meeting transcription endpoints.
///
/// Speaker rename is NOT performed here — it is entirely client-side.
/// There is no `renameSpeakers` method; see `TranscriptStore.renameSpeaker(in:rawKey:to:)`.
enum MeetingAPI {
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
    }

    // MARK: - Submit

    /// Uploads the meeting WAV file to `POST /transcribe/meeting`.
    ///
    /// Computes a `Content-MD5` header (streaming, no full-file RAM load) so the
    /// server can verify upload integrity.  Progress is reported via the `progress`
    /// callback on the main queue as a fraction in `[0.0, 1.0]`.
    ///
    /// A dedicated `URLSession` with an `UploadSessionDelegate` is created per upload
    /// so the delegate can own lifetime and progress reporting. The session is
    /// invalidated after the upload completes.
    ///
    /// Meetings are NEVER retried on transport error — the file is too large for a
    /// transparent retry to be acceptable. They also never fall back to the cloud
    /// Worker (no diarization there). Offline detection in
    /// `MenuBarController.processMeetingUpload` enqueues the WAV to
    /// `PendingUploadsQueue` instead.
    ///
    /// - Parameters:
    ///   - wavURL: Local file URL of the 2-channel 16 kHz WAV to upload.
    ///   - progress: Called on the main queue with upload fraction as data is sent.
    /// - Returns: A `JobID` for use with `poll`, `download`, and `delete`.
    /// - Throws: `ServerError` on any HTTP or transport failure, including
    ///   `.meetingInProgress` (HTTP 429) when the server is already busy. The
    ///   underlying error is wrapped in `MeetingUploadError.transport` so callers
    ///   can recover the offline-classification attempt for fallback decisions.
    static func submit(
        _ wavURL: URL,
        progress: @escaping (Double) -> Void
    ) async throws -> JobID {
        guard let baseURL = Settings.shared.serverURL else {
            throw ServerError.missingConfiguration
        }
        guard let url = URL(string: "/transcribe/meeting", relativeTo: baseURL)?.absoluteURL else {
            throw ServerError.invalidServerURL
        }

        // Compute Content-MD5 over the raw WAV bytes (server compares to the
        // post-multipart-parse file content, NOT the multipart envelope bytes).
        // Streaming so we never load the entire WAV into RAM.
        var hasher = Insecure.MD5()
        let hashHandle = try FileHandle(forReadingFrom: wavURL)
        defer { try? hashHandle.close() }
        while true {
            guard let chunk = try hashHandle.read(upToCount: 1 << 20), !chunk.isEmpty else { break }
            hasher.update(data: chunk)
        }
        let md5Base64 = Data(hasher.finalize()).base64EncodedString()
        try? hashHandle.close()

        // Server endpoint declares ``file: UploadFile`` — that requires
        // multipart/form-data, NOT a raw audio/wav body. Build the multipart
        // envelope as a temp file so we keep streaming behavior (no full-WAV
        // RAM load) and can still use uploadTask(fromFile:).
        let boundary = "wispralt-" + UUID().uuidString
        let prefix = (
            "--\(boundary)\r\n" +
            "Content-Disposition: form-data; name=\"file\"; filename=\"" +
            wavURL.lastPathComponent + "\"\r\n" +
            "Content-Type: audio/wav\r\n\r\n"
        ).data(using: .utf8)!
        let suffix = "\r\n--\(boundary)--\r\n".data(using: .utf8)!

        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("wispralt-upload-\(UUID().uuidString).tmp")
        FileManager.default.createFile(atPath: tempURL.path, contents: nil)
        do {
            let writer = try FileHandle(forWritingTo: tempURL)
            try writer.write(contentsOf: prefix)
            let reader = try FileHandle(forReadingFrom: wavURL)
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
        // Wall-clock cap for the whole upload. Default URLRequest.timeoutInterval is 60s,
        // which kills any upload from a slow uplink (Custom Transcriptions can be hundreds
        // of MB at ~15 KB/s home upload = hours). Six hours covers a realistic worst case.
        request.timeoutInterval = 6 * 60 * 60

        if let apiKey = try? KeychainHelper.getAPIKey() {
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }

        // Perform upload with a dedicated session so we can set a per-upload delegate.
        let (data, response): (Data, HTTPURLResponse)
        do {
            (data, response) = try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>) in

                let delegate = UploadSessionDelegate(
                    progressHandler: progress,
                    continuation: continuation
                )
                // Delegate-based session — the delegate receives progress + response callbacks.
                // Bumped timeouts so long uploads on a slow uplink don't trip the default
                // 60s inactivity timer. Inactivity = no bytes flow either direction; 5 min
                // is generous but still catches an actually-dead connection.
                let uploadConfig = URLSessionConfiguration.default
                uploadConfig.timeoutIntervalForRequest = 300       // 5 min inactivity
                uploadConfig.timeoutIntervalForResource = 6 * 60 * 60  // 6 h wall clock
                let uploadSession = URLSession(
                    configuration: uploadConfig,
                    delegate: delegate,
                    delegateQueue: nil
                )
                delegate.session = uploadSession

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
            Log.info("Meeting submitted — job_id: \(decoded.job_id)", category: "meeting")
            return JobID(raw: decoded.job_id)
        } catch {
            throw ServerError.decoding(error)
        }
    }

    // MARK: - Submit (file — original container, server-side decode)

    /// Uploads any audio/video container to `POST /transcribe/file`.
    ///
    /// The server runs ffmpeg to transcode the upload to a canonical WAV before
    /// queuing the job, then ffprobes the source to detect channel count
    /// (mono → single, stereo → stereo). The client does NOT specify a `mode`
    /// form field — channel detection is server-side.
    ///
    /// Mirrors `submit(_:)`: same multipart envelope, same Content-MD5 header,
    /// same `UploadSessionDelegate` for progress, same long-upload session
    /// configuration (300 s inactivity, 6 h wall clock, 6 h request timeout).
    /// The only differences are the target URL and that the bytes uploaded are
    /// the user's source container as-is — no client-side transcoding.
    ///
    /// - Parameters:
    ///   - sourceURL: Local file URL of the source container (m4a, mp3, mp4, …).
    ///   - progress: Called on the main queue with upload fraction as data is sent.
    /// - Returns: A `JobID` for use with `poll`, `download`, and `delete`.
    /// - Throws: `ServerError` on any HTTP or transport failure.
    static func submitFile(
        _ sourceURL: URL,
        progress: @escaping (Double) -> Void
    ) async throws -> JobID {
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
        let prefix = (
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
            try writer.write(contentsOf: prefix)
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
        // Wall-clock cap for the whole upload — see `submit(_:)` rationale.
        request.timeoutInterval = 6 * 60 * 60

        if let apiKey = try? KeychainHelper.getAPIKey() {
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }

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
            Log.info("File submitted — job_id: \(decoded.job_id)", category: "meeting")
            return JobID(raw: decoded.job_id)
        } catch {
            throw ServerError.decoding(error)
        }
    }

    // MARK: - Poll

    /// Polls `GET /transcribe/meeting/{id}` for the current job status.
    ///
    /// - Parameter id: Job identifier from `submit`.
    /// - Returns: A `JobStatus` value representing the current state.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func poll(_ id: JobID) async throws -> JobStatus {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/meeting/\(id.raw)"
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
    /// `GET /transcribe/meeting/{id}/download/{format}`.
    ///
    /// - Parameters:
    ///   - id: Job identifier.
    ///   - format: One of `"json"`, `"srt"`, `"vtt"`, `"txt"`.
    /// - Returns: The raw file bytes.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func download(_ id: JobID, format: String) async throws -> Data {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/meeting/\(id.raw)/download/\(format)"
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        return data
    }

    // MARK: - Delete

    /// Calls `DELETE /transcribe/meeting/{id}` to clean up server-side staging files
    /// after the client has successfully saved the transcript locally.
    ///
    /// - Parameter id: Job identifier.
    /// - Throws: `ServerError` on HTTP or transport failure.
    static func delete(_ id: JobID) async throws {
        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/meeting/\(id.raw)",
            method: "DELETE"
        )
        _ = try await ServerClient.shared.execute(request)
        Log.info("Server-side job \(id.raw) deleted.", category: "meeting")
    }

    // MARK: - Private helpers

    private static func mapStatus(_ response: PollResponse) -> JobStatus {
        switch response.status {
        case "pending":
            return .pending
        case "running":
            return .running
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
