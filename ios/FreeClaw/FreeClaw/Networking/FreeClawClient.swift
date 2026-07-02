import Foundation

/// Talks to a single FreeClaw agent (Flask/main.py). Auth is a session
/// cookie set by POST /login — there's no bearer token for the normal UI
/// routes, so this client relies on cookie storage the same way a browser
/// would. Cookie storage is scoped to the shared App Group container (not
/// `.shared`) so a login here is also visible to the widget extension's own
/// `FreeClawClient` instance — both must use this same group identifier.
final class FreeClawClient: NSObject {
    private(set) var baseURL: URL = URL(string: "http://localhost")!
    private var session: URLSession!
    private var noRedirectSession: URLSession!
    private let cookieStorage = HTTPCookieStorage(forGroupContainerIdentifier: "group.dev.eedeb.freeclaw")

    /// `timeoutInterval` defaults to 20s for the main app; the widget
    /// extension passes a shorter value so an unreachable LAN server fails
    /// fast instead of stalling the widget configuration UI.
    init(timeoutInterval: TimeInterval = 20) {
        super.init()
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = cookieStorage
        config.timeoutIntervalForRequest = timeoutInterval
        session = URLSession(configuration: config)

        // A second session, with redirects disabled, purely to observe the
        // 302 Flask sends on a successful /login (vs. the 200 it sends back
        // when re-rendering login.html with an error).
        let noRedirectConfig = URLSessionConfiguration.default
        noRedirectConfig.httpCookieStorage = cookieStorage
        noRedirectConfig.timeoutIntervalForRequest = timeoutInterval
        noRedirectSession = URLSession(configuration: noRedirectConfig, delegate: self, delegateQueue: nil)
    }

    func configure(baseURL: URL) {
        self.baseURL = baseURL
    }

    func clearSession() {
        for cookie in cookieStorage.cookies(for: baseURL) ?? [] {
            cookieStorage.deleteCookie(cookie)
        }
    }

    // MARK: - Auth

    func login(password: String) async throws -> Bool {
        var request = URLRequest(url: baseURL.appendingPathComponent("login"))
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let encoded = password.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? ""
        request.httpBody = Data("password=\(encoded)".utf8)

        let (_, response) = try await noRedirectSession.data(for: request)
        guard let http = response as? HTTPURLResponse else { return false }
        return http.statusCode == 302
    }

    func logout() async {
        var request = URLRequest(url: baseURL.appendingPathComponent("logout"))
        request.httpMethod = "GET"
        _ = try? await noRedirectSession.data(for: request)
        clearSession()
    }

    // MARK: - Users

    func fetchUsers() async throws -> [UserSummary] {
        try await decode(UsersResponse.self, from: getJSON("api/users")).users
    }

    func createUser(name: String) async throws {
        let body = try JSONEncoder().encode(["name": name])
        _ = try await postJSON("api/users", body: body)
    }

    func deleteUser(name: String) async throws {
        try await delete("api/users/\(pathEscape(name))")
    }

    func fetchConversations(user: String) async throws -> [ConversationSummary] {
        try await decode(
            ConversationsResponse.self,
            from: getJSON("api/users/\(pathEscape(user))/conversations")
        ).conversations
    }

    func createConversation(user: String) async throws -> String {
        try await decode(
            NewConversationResponse.self,
            from: postJSON("api/users/\(pathEscape(user))/conversations", body: nil)
        ).id
    }

    func deleteConversation(user: String, id: String) async throws {
        try await delete("api/users/\(pathEscape(user))/conversations/\(pathEscape(id))")
    }

    // MARK: - Active conversation

    /// Mirrors visiting /chat?user=&conv= in a browser: points this
    /// session's server-side "current user / current chat" at the given
    /// conversation so subsequent POST /chat, /reset, /upload calls land
    /// on the right one.
    func openChat(user: String, conversationId: String) async throws {
        var components = URLComponents(url: baseURL.appendingPathComponent("chat"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "user", value: user),
            URLQueryItem(name: "conv", value: conversationId)
        ]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        let (data, response) = try await session.data(for: request)
        try Self.checkOK(response, data: data)
    }

    func fetchActiveConversation() async throws -> ActiveConversation {
        try await decode(ActiveConversation.self, from: getJSON("api/conversation"))
    }

    func resetConversation() async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("reset"))
        request.httpMethod = "POST"
        let (data, response) = try await session.data(for: request)
        try Self.checkOK(response, data: data)
    }

    func uploadFile(data: Data, filename: String, mimeType: String) async throws -> String {
        let boundary = "FreeClawBoundary-\(UUID().uuidString)"
        var request = URLRequest(url: baseURL.appendingPathComponent("upload"))
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: \(mimeType)\r\n\r\n".data(using: .utf8)!)
        body.append(data)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        request.httpBody = body

        let (respData, response) = try await session.data(for: request)
        try Self.checkOK(response, data: respData)
        return try decode(UploadResponse.self, from: respData).path
    }

    // MARK: - Streaming chat

    func sendMessage(_ text: String) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = URLRequest(url: baseURL.appendingPathComponent("chat"))
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.httpBody = try JSONEncoder().encode(["message": text])

                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw FreeClawError.badResponse
                    }
                    if http.statusCode == 401 {
                        for try await _ in bytes {}
                        throw FreeClawError.unauthorized
                    }

                    let contentType = http.value(forHTTPHeaderField: "Content-Type") ?? ""
                    if contentType.contains("text/event-stream") {
                        for try await line in bytes.lines {
                            guard line.hasPrefix("data:") else { continue }
                            let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                            guard !payload.isEmpty, let data = payload.data(using: .utf8) else { continue }
                            guard let raw = try? JSONDecoder().decode(RawEvent.self, from: data) else { continue }
                            let event = Self.streamEvent(from: raw)
                            continuation.yield(event)
                            if case .done = event { break }
                        }
                    } else {
                        // Slash-commands and pre-stream errors come back as one JSON object.
                        var data = Data()
                        for try await byte in bytes { data.append(byte) }
                        if let obj = try? JSONDecoder().decode(SimpleChatResponse.self, from: data) {
                            if let error = obj.error {
                                continuation.yield(.error(error))
                            } else if let conversation = obj.conversation {
                                continuation.yield(.done(conversation))
                            } else {
                                continuation.yield(.token(obj.response ?? ""))
                                continuation.yield(.done([]))
                            }
                        } else {
                            continuation.yield(.error("Unexpected server response."))
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func streamEvent(from raw: RawEvent) -> ChatStreamEvent {
        switch raw.type {
        case "token": return .token(raw.text ?? "")
        case "tool_call": return .toolCall(name: raw.name ?? "tool", url: raw.arguments?.url)
        case "tool_result": return .toolResult(name: raw.name ?? "tool")
        case "error": return .error(raw.error ?? "Unknown error")
        case "done": return .done(raw.conversation ?? [])
        default: return .error("Unknown event: \(raw.type)")
        }
    }

    // MARK: - Low-level helpers

    private func getJSON(_ path: String) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "GET"
        let (data, response) = try await session.data(for: request)
        try Self.checkOK(response, data: data)
        return data
    }

    private func postJSON(_ path: String, body: Data?) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        try Self.checkOK(response, data: data)
        return data
    }

    private func delete(_ path: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "DELETE"
        let (data, response) = try await session.data(for: request)
        try Self.checkOK(response, data: data)
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw FreeClawError.decoding
        }
    }

    private func pathEscape(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }

    private static func checkOK(_ response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else { throw FreeClawError.badResponse }
        if http.statusCode == 401 { throw FreeClawError.unauthorized }
        guard (200...299).contains(http.statusCode) else {
            if let obj = try? JSONDecoder().decode(ErrorResponse.self, from: data) {
                throw FreeClawError.server(obj.error)
            }
            throw FreeClawError.server("Server returned status \(http.statusCode).")
        }
    }
}

extension FreeClawClient: URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest
    ) async -> URLRequest? {
        nil // don't follow — the 302 itself is how login() detects success
    }
}

private extension CharacterSet {
    /// .urlQueryAllowed still permits "&" and "+", which would corrupt a
    /// form-urlencoded body; this is the stricter set application/
    /// x-www-form-urlencoded actually needs for a single value.
    static let urlQueryValueAllowed: CharacterSet = {
        var set = CharacterSet.alphanumerics
        set.insert(charactersIn: "-._~")
        return set
    }()
}
