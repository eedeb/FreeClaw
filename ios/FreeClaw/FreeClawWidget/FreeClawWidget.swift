import WidgetKit
import SwiftUI

struct FreeClawWidgetEntry: TimelineEntry {
    let date: Date
    let userName: String
    let conversationTitle: String
    let deepLinkURL: URL
}

/// The entry only needs the values already captured on the configuration
/// intent (`configuration.user`/`configuration.conversation`) — no network
/// call here, since the widget's content (a user name + chat title) never
/// changes on its own between reconfigurations, and hitting the server on
/// every OS-triggered timeline refresh would be wasteful and unreliable off
/// the home LAN.
struct FreeClawWidgetProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> FreeClawWidgetEntry {
        FreeClawWidgetEntry(
            date: Date(),
            userName: "User",
            conversationTitle: "Conversation",
            deepLinkURL: Self.deepLink(user: "", conversationId: "")
        )
    }

    func snapshot(for configuration: SelectConversationIntent, in context: Context) async -> FreeClawWidgetEntry {
        entry(for: configuration)
    }

    func timeline(for configuration: SelectConversationIntent, in context: Context) async -> Timeline<FreeClawWidgetEntry> {
        Timeline(entries: [entry(for: configuration)], policy: .never)
    }

    private func entry(for configuration: SelectConversationIntent) -> FreeClawWidgetEntry {
        FreeClawWidgetEntry(
            date: Date(),
            userName: configuration.user.name,
            conversationTitle: configuration.conversation.title,
            deepLinkURL: Self.deepLink(user: configuration.user.name, conversationId: configuration.conversation.id)
        )
    }

    private static func deepLink(user: String, conversationId: String) -> URL {
        var components = URLComponents()
        components.scheme = "freeclaw"
        components.host = "voice"
        components.queryItems = [
            URLQueryItem(name: "user", value: user),
            URLQueryItem(name: "conv", value: conversationId)
        ]
        return components.url ?? URL(string: "freeclaw://voice")!
    }
}

struct FreeClawWidget: Widget {
    let kind = "dev.eedeb.freeclaw.widget.voice"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: SelectConversationIntent.self, provider: FreeClawWidgetProvider()) { entry in
            FreeClawWidgetEntryView(entry: entry)
        }
        .configurationDisplayName("Push to Talk")
        .description("Tap to jump straight into a conversation with voice mode on.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
