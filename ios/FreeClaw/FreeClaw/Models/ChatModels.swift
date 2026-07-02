import Foundation

// MARK: - Raw conversation wire format
//
// Mirrors the OpenAI-style message array the Flask server stores and returns
// verbatim from /api/conversation and the "done" SSE event (see
// Flask/templates/chat.html renderConversation()).

struct RawMessage: Decodable {
    var role: String
    var content: MessageContent?
    var toolCalls: [RawToolCall]?
    var toolCallId: String?
    var name: String?

    enum CodingKeys: String, CodingKey {
        case role, content, name
        case toolCalls = "tool_calls"
        case toolCallId = "tool_call_id"
    }
}

enum MessageContent: Decodable {
    case text(String)
    case blocks([ContentBlock])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let str = try? container.decode(String.self) {
            self = .text(str)
        } else if let blocks = try? container.decode([ContentBlock].self) {
            self = .blocks(blocks)
        } else {
            self = .text("")
        }
    }

    var plainText: String {
        switch self {
        case .text(let s):
            return s
        case .blocks(let blocks):
            return blocks.compactMap(\.text).joined(separator: "\n")
        }
    }
}

struct ContentBlock: Decodable {
    var type: String?
    var text: String?
}

struct RawToolCall: Decodable {
    var id: String
    var function: RawFunctionCall
}

struct RawFunctionCall: Decodable {
    var name: String
    var arguments: String
}

// MARK: - Display model
//
// A flattened, render-ready projection of the raw message list — one case
// per bubble/card the chat screen actually draws. Built fresh every time a
// full conversation snapshot comes back from the server.

enum ChatDisplayItem: Identifiable {
    case user(id: String, text: String)
    case agent(id: String, text: String)
    case tool(id: String, name: String, argsJSON: String, resultText: String, isError: Bool)

    var id: String {
        switch self {
        case .user(let id, _), .agent(let id, _):
            return id
        case .tool(let id, _, _, _, _):
            return id
        }
    }
}

func buildDisplayItems(from messages: [RawMessage]) -> [ChatDisplayItem] {
    var toolResults: [String: String] = [:]
    for message in messages where message.role == "tool" {
        guard let id = message.toolCallId else { continue }
        toolResults[id] = message.content?.plainText ?? ""
    }

    var items: [ChatDisplayItem] = []
    var counter = 0
    for message in messages {
        if message.role == "system" || message.role == "tool" { continue }
        counter += 1

        if message.role == "user" {
            items.append(.user(id: "u\(counter)", text: message.content?.plainText ?? ""))
        } else if message.role == "assistant" {
            for call in message.toolCalls ?? [] {
                let resultText = toolResults[call.id] ?? "(no result)"
                let isError = resultText.lowercased().hasPrefix("error")
                items.append(.tool(
                    id: call.id,
                    name: call.function.name,
                    argsJSON: call.function.arguments,
                    resultText: resultText,
                    isError: isError
                ))
            }
            let text = message.content?.plainText ?? ""
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                items.append(.agent(id: "a\(counter)", text: text))
            }
        }
    }
    return items
}

// MARK: - Attachments

struct PendingAttachment: Equatable {
    var filename: String
    var path: String
    var isImage: Bool
}
