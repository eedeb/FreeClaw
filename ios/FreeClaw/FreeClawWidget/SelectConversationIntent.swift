import AppIntents

// MARK: - User entity

struct UserEntity: AppEntity, Identifiable {
    let id: String  // FreeClaw identifies users by name, not a separate id
    let name: String
    var chatCount: Int

    static var typeDisplayRepresentation: TypeDisplayRepresentation { "FreeClaw User" }
    static var defaultQuery = UserEntityQuery()

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(name)")
    }
}

struct UserEntityQuery: EntityQuery {
    func entities(for identifiers: [UserEntity.ID]) async throws -> [UserEntity] {
        try await Self.fetchAll().filter { identifiers.contains($0.id) }
    }

    func suggestedEntities() async throws -> [UserEntity] {
        try await Self.fetchAll()
    }

    private static func fetchAll() async throws -> [UserEntity] {
        guard let client = WidgetServerAccess.makeClient(),
              let summaries = try? await client.fetchUsers()
        else { return [] }
        return summaries.map { UserEntity(id: $0.name, name: $0.name, chatCount: $0.chatCount) }
    }
}

// MARK: - Conversation entity

struct ConversationEntity: AppEntity, Identifiable {
    let id: String
    let title: String
    let user: String

    static var typeDisplayRepresentation: TypeDisplayRepresentation { "FreeClaw Conversation" }
    static var defaultQuery = ConversationEntityQuery()

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(title)")
    }
}

struct ConversationEntityQuery: EntityQuery {
    // Ties this query to whichever `user` the person has already picked in
    // SelectConversationIntent's configuration sheet, so the conversation
    // picker only shows that user's chats.
    @IntentParameterDependency<SelectConversationIntent>(\.$user)
    var selectConversationIntent

    func entities(for identifiers: [ConversationEntity.ID]) async throws -> [ConversationEntity] {
        try await Self.fetchAll(user: selectConversationIntent?.user).filter { identifiers.contains($0.id) }
    }

    func suggestedEntities() async throws -> [ConversationEntity] {
        try await Self.fetchAll(user: selectConversationIntent?.user)
    }

    private static func fetchAll(user: UserEntity?) async throws -> [ConversationEntity] {
        guard let user, let client = WidgetServerAccess.makeClient(),
              let summaries = try? await client.fetchConversations(user: user.name)
        else { return [] }
        return summaries.map { ConversationEntity(id: $0.id, title: $0.title, user: user.name) }
    }
}

// MARK: - Configuration intent

struct SelectConversationIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource { "Select Conversation" }
    static var description: IntentDescription {
        IntentDescription("Choose which FreeClaw user and conversation this widget opens.")
    }

    @Parameter(title: "User")
    var user: UserEntity

    @Parameter(title: "Conversation")
    var conversation: ConversationEntity
}
