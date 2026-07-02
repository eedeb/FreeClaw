import SwiftUI

/// Home screen once connected — one FreeClaw agent can host several
/// independent users, each with their own memory and chat history.
struct UsersListView: View {
    @EnvironmentObject private var store: ServerStore
    @State private var users: [UserSummary] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var showAddUser = false
    @State private var newUserName = ""
    @State private var path = NavigationPath()

    var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                FCTheme.background.ignoresSafeArea()
                content
            }
            .navigationTitle("FreeClaw")
            .toolbarBackground(FCTheme.background, for: .navigationBar)
            .toolbarColorScheme(.dark, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    NavigationLink(destination: SettingsView()) {
                        Image(systemName: "gearshape")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showAddUser = true
                    } label: {
                        Image(systemName: "plus")
                    }
                }
            }
            .refreshable { await load() }
            .task {
                await load()
                consumePendingDeepLinkIfPossible()
            }
            .onChange(of: store.pendingVoiceTarget) { _ in
                consumePendingDeepLinkIfPossible()
            }
            .navigationDestination(for: UserSummary.self) { user in
                ConversationsListView(user: user.name)
            }
            .alert("New User", isPresented: $showAddUser) {
                TextField("Name", text: $newUserName)
                Button("Cancel", role: .cancel) { newUserName = "" }
                Button("Create") { Task { await createUser() } }
            } message: {
                Text("Letters, numbers, spaces, - or _")
            }
        }
    }

    /// If a widget deep link is waiting and its user is in the loaded list,
    /// push straight to that user's conversation list (which itself opens
    /// the right conversation — see ConversationsListView).
    private func consumePendingDeepLinkIfPossible() {
        guard let target = store.pendingVoiceTarget,
              let matchedUser = users.first(where: { $0.name == target.user })
        else { return }
        path.append(matchedUser)
    }

    @ViewBuilder
    private var content: some View {
        if isLoading {
            ProgressView().tint(FCTheme.accent)
        } else if let errorMessage {
            ErrorStateView(message: errorMessage) { Task { await load() } }
        } else if users.isEmpty {
            emptyState
        } else {
            List {
                ForEach(users) { user in
                    NavigationLink(value: user) {
                        UserRow(user: user)
                    }
                    .listRowBackground(FCTheme.surface)
                }
                .onDelete { offsets in
                    Task { await deleteUsers(at: offsets) }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "person.crop.circle.badge.plus")
                .font(.system(size: 36))
                .foregroundStyle(FCTheme.muted)
            Text("No users yet")
                .foregroundStyle(FCTheme.text)
            Text("Tap + to add one")
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(FCTheme.muted)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func load() async {
        errorMessage = nil
        do {
            users = try await store.client.fetchUsers()
        } catch {
            handle(error)
        }
        isLoading = false
    }

    private func createUser() async {
        let name = newUserName.trimmingCharacters(in: .whitespaces)
        newUserName = ""
        guard !name.isEmpty else { return }
        do {
            try await store.client.createUser(name: name)
            await load()
        } catch {
            handle(error)
        }
    }

    private func deleteUsers(at offsets: IndexSet) async {
        for index in offsets {
            try? await store.client.deleteUser(name: users[index].name)
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

private struct UserRow: View {
    var user: UserSummary

    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                Circle().fill(FCTheme.userBubble)
                Circle().stroke(FCTheme.userBorder, lineWidth: 1)
                Text(initials)
                    .font(.system(size: 13, weight: .semibold, design: .monospaced))
                    .foregroundStyle(FCTheme.accent)
            }
            .frame(width: 36, height: 36)

            VStack(alignment: .leading, spacing: 2) {
                Text(user.name)
                    .font(.system(.body, design: .rounded).weight(.semibold))
                    .foregroundStyle(FCTheme.text)
                Text("\(user.chatCount) chat\(user.chatCount == 1 ? "" : "s")")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(FCTheme.muted)
            }
        }
        .padding(.vertical, 4)
    }

    private var initials: String {
        String(user.name.trimmingCharacters(in: .whitespaces).prefix(2)).uppercased()
    }
}
