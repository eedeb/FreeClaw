import Foundation

// MARK: - REST payloads

struct UserSummary: Identifiable, Decodable, Hashable {
    var name: String
    var chatCount: Int
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case chatCount = "chat_count"
    }
}

struct ConversationSummary: Identifiable, Decodable, Hashable {
    var id: String
    var title: String
    var updatedAt: Double

    enum CodingKeys: String, CodingKey {
        case id, title
        case updatedAt = "updated_at"
    }
}

struct UsersResponse: Decodable { var users: [UserSummary] }
struct ConversationsResponse: Decodable { var conversations: [ConversationSummary] }
struct NewConversationResponse: Decodable { var id: String }
struct UploadResponse: Decodable { var path: String; var filename: String }
struct ErrorResponse: Decodable { var error: String }

struct ActiveConversation: Decodable {
    var user: String
    var id: String
    var title: String?
    var messages: [RawMessage]
}

struct SimpleChatResponse: Decodable {
    var response: String?
    var error: String?
    var conversation: [RawMessage]?
}

/// One SSE frame from POST /chat (see Flask/main.py `generate()` and
/// chat.html's stream reader).
struct RawEvent: Decodable {
    var type: String
    var text: String?
    var name: String?
    var error: String?
    var conversation: [RawMessage]?
}

// MARK: - Stream events surfaced to the UI

enum ChatStreamEvent {
    case token(String)
    case toolCall(name: String)
    case toolResult(name: String)
    case error(String)
    case done([RawMessage])
}

// MARK: - Errors

enum FreeClawError: LocalizedError {
    case badResponse
    case unauthorized
    case server(String)
    case decoding

    var errorDescription: String? {
        switch self {
        case .badResponse:
            return "The server sent an unexpected response."
        case .unauthorized:
            return "Your session expired. Please log in again."
        case .server(let message):
            return message
        case .decoding:
            return "Couldn't understand the server's response."
        }
    }
}
