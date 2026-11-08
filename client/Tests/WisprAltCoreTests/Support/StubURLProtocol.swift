import Foundation
import Testing

/// Parent of every suite that uses `StubURLProtocol`. Its `.serialized`
/// trait applies to the nested suites, so they never run concurrently with
/// each other and clobber the static stub state.
@Suite(.serialized) enum StubURLSuites {}

/// URLProtocol stub for provider tests. `responses` is consumed in order, one
/// per request; `.hang` never answers (the request only ends when cancelled).
/// State is static, so every suite that uses it is nested in `StubURLSuites`
/// and calls `reset()` first.
final class StubURLProtocol: URLProtocol {
    enum Reply {
        case http(Int, Data)
        case failure(URLError)
        case hang
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var queue: [Reply] = []
    nonisolated(unsafe) private static var seen: [URLRequest] = []
    nonisolated(unsafe) private static var stops = 0

    static func reset(_ replies: [Reply]) {
        lock.lock(); defer { lock.unlock() }
        queue = replies
        seen = []
        stops = 0
    }

    static var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return seen }
    static var stopLoadingCount: Int { lock.lock(); defer { lock.unlock() }; return stops }

    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: config)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        var recorded = request
        if recorded.httpBody == nil, let stream = request.httpBodyStream { recorded.httpBody = Self.drain(stream) }
        Self.seen.append(recorded)
        let reply = Self.queue.isEmpty ? .hang : Self.queue.removeFirst()
        Self.lock.unlock()
        switch reply {
        case .http(let status, let body):
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        case .failure(let error):
            client?.urlProtocol(self, didFailWithError: error)
        case .hang:
            break
        }
    }

    override func stopLoading() {
        Self.lock.lock(); Self.stops += 1; Self.lock.unlock()
    }

    private static func drain(_ stream: InputStream) -> Data {
        stream.open(); defer { stream.close() }
        var data = Data()
        var buf = [UInt8](repeating: 0, count: 65_536)
        while stream.hasBytesAvailable {
            let n = stream.read(&buf, maxLength: buf.count)
            if n <= 0 { break }
            data.append(buf, count: n)
        }
        return data
    }
}
