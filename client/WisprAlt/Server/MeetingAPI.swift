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
    /// transparent retry to be acceptable.
    ///
    /// TODO (G3): Add resumable upload support.  On transport error, persist the WAV
    /// path to `~/Library/Application Support/co.wispralt/pending-uploads/<uuid>.path`
    /// so the user can resubmit on next launch without re-recording.  A background
    /// URLSession with resume-data capture from the delegate is the full solution.
    ///
    /// - Parameters:
    ///   - wavURL: Local file URL of the 2-channel 16 kHz WAV to upload.
    ///   - progress: Called on the main queue with upload fraction as data is sent.
    /// - Returns: A `JobID` for use with `poll`, `download`, and `delete`.
    /// - Throws: `ServerError` on any HTTP or transport failure, including
    ///   `.meetingInProgress` (HTTP 429) when the server is already busy.
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

        // Compute Content-MD5 via streaming so we never load the entire WAV into RAM.
        // Large meetings can be hundreds of MB; loading them whole causes peak-RSS spikes.
        var hasher = Insecure.MD5()
        let handle = try FileHandle(forReadingFrom: wavURL)
        defer { try? handle.close() }
        while true {
            guard let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty else { break }
            hasher.update(data: chunk)
        }
        let md5Base64 = Data(hasher.finalize()).base64EncodedString()

        // Determine file size separately (needed for Content-Length header).
        let fileSize = try FileManager.default.attributesOfItem(atPath: wavURL.path)[.size] as? Int ?? 0

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        request.setValue(md5Base64, forHTTPHeaderField: "Content-MD5")
        request.setValue(String(fileSize), forHTTPHeaderField: "Content-Length")

        if let apiKey = try? KeychainHelper.getAPIKey() {
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }

        // Perform upload with a dedicated session so we can set a per-upload delegate.
        let (data, response) = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>) in

            let delegate = UploadSessionDelegate(
                progressHandler: progress,
                continuation: continuation
            )
            // Delegate-based session — the delegate receives progress + response callbacks.
            let uploadSession = URLSession(
                configuration: .default,
                delegate: delegate,
                delegateQueue: nil
            )
            delegate.session = uploadSession

            let task = uploadSession.uploadTask(with: request, fromFile: wavURL)
            task.resume()
        }

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
