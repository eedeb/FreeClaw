import SwiftUI

@main
struct FreeClawApp: App {
    @StateObject private var store = ServerStore()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .tint(FCTheme.accent)
                .preferredColorScheme(.dark)
                .onOpenURL { handleOpenURL($0) }
        }
    }

    /// Handles freeclaw://voice?user=&conv= links from the widget: stashes
    /// the target on the store, which UsersListView/ConversationsListView
    /// pick up to navigate straight to that conversation with voice mode on.
    private func handleOpenURL(_ url: URL) {
        guard url.host == "voice",
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let user = components.queryItems?.first(where: { $0.name == "user" })?.value,
              let conv = components.queryItems?.first(where: { $0.name == "conv" })?.value,
              !user.isEmpty, !conv.isEmpty
        else { return }
        store.pendingVoiceTarget = PendingVoiceTarget(user: user, conversationId: conv)
    }
}
