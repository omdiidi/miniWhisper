import Foundation

/// Namespace for the dictation transcription endpoint.
enum DictationAPI {
    // MARK: - Response model

    private struct TranscribeResponse: Decodable {
        let text: String
        let model_id: String
        // Server returns this as a float (e.g. 119.94 ms), so we decode as Double.
        // Decoding as Int would silently fail with a "data not in correct format"
        // error because Foundation's JSONDecoder rejects float→Int coercion.
        let duration_ms: Double
    }

    // MARK: - Public API

    /// Sends PCM WAV data to `POST /transcribe/dictate` and returns the transcribed text.
    ///
    /// The request is sent as `multipart/form-data` with a single field named `file`
    /// containing the WAV payload. On success, the `text` field of the JSON response
    /// is returned.
    ///
    /// - Parameter wavData: Raw WAV-encoded audio bytes captured during dictation.
    /// - Returns: Transcribed text string. May be empty if the server produced no output.
    /// - Throws: `ServerError` on any HTTP or transport failure.
    static func transcribe(_ wavData: Data) async throws -> String {
        let boundary = UUID().uuidString
        let body = buildMultipartBody(wavData: wavData, boundary: boundary)

        let request = try ServerClient.shared.buildRequest(
            path: "/transcribe/dictate",
            method: "POST",
            body: body,
            contentType: "multipart/form-data; boundary=\(boundary)"
        )

        let (data, _) = try await ServerClient.shared.execute(request, retryOnReset: true)

        do {
            let decoded = try JSONDecoder().decode(TranscribeResponse.self, from: data)
            Log.debug(
                "Dictation response: \"\(decoded.text)\" in \(decoded.duration_ms)ms via \(decoded.model_id)",
                category: "dictation"
            )
            return decoded.text
        } catch {
            throw ServerError.decoding(error)
        }
    }

    // MARK: - Private helpers

    /// Builds a minimal `multipart/form-data` body for a single file field named `file`.
    private static func buildMultipartBody(wavData: Data, boundary: String) -> Data {
        var body = Data()
        let crlf = "\r\n"

        // Opening boundary.
        body.append("--\(boundary)\(crlf)".utf8Data)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\(crlf)".utf8Data)
        body.append("Content-Type: audio/wav\(crlf)".utf8Data)
        body.append(crlf.utf8Data)
        body.append(wavData)
        body.append(crlf.utf8Data)

        // Closing boundary.
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
