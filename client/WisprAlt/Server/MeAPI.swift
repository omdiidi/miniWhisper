import Foundation

/// Response shape for `GET /me` and `PATCH /me`.
struct MeResponse: Decodable {
    let label: String
    let display_name: String?
    let role: String
    let created_at: String
    let last_seen_at: String?
}

/// Namespace for the `/me` self-service endpoint.
///
/// Every call follows the same pattern as `DictationAPI` and `MeetingAPI`:
/// build via `ServerClient.shared.buildRequest`, execute via
/// `ServerClient.shared.execute`, destructure the `(Data, HTTPURLResponse)` tuple,
/// then decode `data` with `JSONDecoder`.
enum MeAPI {
    /// `GET /me` — fetch the caller's identity (label, display_name, role, timestamps).
    static func get() async throws -> MeResponse {
        let request = try ServerClient.shared.buildRequest(path: "/me", method: "GET")
        // ServerClient.execute returns (Data, HTTPURLResponse) — destructure both
        // so the tuple isn't fed to JSONDecoder. This is the same pattern used in
        // DictationAPI.swift and MeetingAPI.swift.
        let (data, _) = try await ServerClient.shared.execute(request)
        do {
            return try JSONDecoder().decode(MeResponse.self, from: data)
        } catch {
            throw ServerError.decoding(error)
        }
    }

    /// `PATCH /me` — set or clear the caller's display_name.
    ///
    /// Pass `nil` to clear (serializes as JSON `null`, NOT a missing field).
    static func patchDisplayName(_ name: String?) async throws -> MeResponse {
        // Use Codable Encodable so Optional<String> nil correctly serializes as JSON null.
        // JSONSerialization.data(withJSONObject:) with Optional.none raises
        // NSInvalidArgumentException; Encodable handles `nil` cleanly as `null`.
        struct PatchBody: Encodable { let display_name: String? }
        let body = try JSONEncoder().encode(PatchBody(display_name: name))

        let request = try ServerClient.shared.buildRequest(
            path: "/me",
            method: "PATCH",
            body: body,
            contentType: "application/json",
            additionalHeaders: ["Accept": "application/json"]
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        do {
            return try JSONDecoder().decode(MeResponse.self, from: data)
        } catch {
            throw ServerError.decoding(error)
        }
    }
}
