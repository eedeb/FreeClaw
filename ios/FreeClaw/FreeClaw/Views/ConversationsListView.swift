import SwiftUI

/// Chat list for a single user — mirrors the "Chats — <user>" panel in
/// index.html, but as a native swipe-to-delete list with push navigation.
struct ConversationsListView: View {
    @EnvironmentObject private var store: ServerStore
    let user: String

    @State private var conversations: [ConversationSummary] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var isCreating = false
    @State private var selectedConversation: ConversationSummary?
    @State private var showChat = false

    var body: some View {
        ZStack {
            FCTheme.background.ignoresSafeArea()
            content
        }
        .navigationTitle(user)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(FCTheme.background, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await createConversation() }
                } label: {
                    if isCreating {
                        ProgressView()
                    } else {
                        Image(systemName: "square.and.pencil")
                    }
                }
                .disabled(isCreating)
            }
        }
        .refreshable { await load() }
        .task { await load() }
        .navigationDestination(isPresented: $showChat) {
            if let selectedConversation {
                ChatView(user: user, conversationId: selectedConversation.id, title: selectedConversation.title)
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if isLoading {
            ProgressView().tint(FCTheme.accent)
        } else if let errorMessage {
            ErrorStateView(message: errorMessage) { Task { await load() } }
        } else if conversations.isEmpty {
            emptyState
        } else {
            List {
                ForEach(conversations) { conv in
                    Button {
                        open(conv)
                    } label: {
                        ConversationRow(conversation: conv)
                    }
                    .listRowBackground(FCTheme.surface)
                }
                .onDelete { offsets in
                    Task { await deleteConversations(at: offsets) }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 34))
                .foregroundStyle(FCTheme.muted)
            Text("No chats yet")
                .foregroundStyle(FCTheme.text)
            Button("Start a new chat") { Task { await createConversation() } }
                .font(.system(.footnote, design: .monospaced).weight(.semibold))
                .foregroundStyle(FCTheme.accent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func open(_ conversation: ConversationSummary) {
        selectedConversation = conversation
        showChat = true
    }

    private func load() async {
        errorMessage = nil
        do {
            conversations = try await store.client.fetchConversations(user: user)
        } catch {
            handle(error)
        }
        isLoading = false
    }

    private func createConversation() async {
        isCreating = true
        do {
            let id = try await store.client.createConversation(user: user)
            open(ConversationSummary(id: id, title: "New chat", updatedAt: Date().timeIntervalSince1970))
        } catch {
            handle(error)
        }
        isCreating = false
    }

    private func deleteConversations(at offsets: IndexSet) async {
        for index in offsets {
            try? await store.client.deleteConversation(user: user, id: conversations[index].id)
        }
        await load()
    }

    private func handle(_ error: Error) {
        if case FreeClawError.unauthorized = error {
            store.sessionExpired()
            return
        }
        errorMessage = error.localizedDescription
    }
}

private struct ConversationRow: View {
    var conversation: ConversationSummary

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bubble.left.fill")
                .foregroundStyle(FCTheme.accentDim)
                .font(.system(size: 14))
            VStack(alignment: .leading, spacing: 2) {
                Text(conversation.title)
                    .font(.system(.subheadline))
                    .foregroundStyle(FCTheme.text)
                    .lineLimit(1)
                if conversation.updatedAt > 0 {
                    Text(Date(timeIntervalSince1970: conversation.updatedAt), format: .relative(presentation: .named))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(FCTheme.muted)
                }
            }
            Spacer()
        }
        .padding(.vertical, 4)
    }
}
