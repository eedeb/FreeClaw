import SwiftUI
import UniformTypeIdentifiers

/// The main chat screen: loads history for a conversation, streams new
/// replies token-by-token over SSE, and shows tool calls live as they run.
struct ChatView: View {
    @EnvironmentObject private var store: ServerStore
    let user: String
    let conversationId: String
    @State var title: String

    @State private var items: [ChatDisplayItem] = []
    @State private var isLoading = true
    @State private var loadError: String?

    @State private var inputText = ""
    @State private var isSending = false
    @State private var streamingText = ""
    @State private var isStreamingText = false
    @State private var liveToolName: String?
    @State private var isToolRunning = false
    @State private var isThinking = false
    @State private var sendError: String?

    @State private var pendingAttachment: PendingAttachment?
    @State private var isUploading = false
    @State private var showFileImporter = false
    @State private var showResetConfirm = false

    @FocusState private var inputFocused: Bool

    private var canSend: Bool {
        (!inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pendingAttachment != nil)
            && !isSending && !isUploading
    }

    var body: some View {
        ZStack {
            FCTheme.background.ignoresSafeArea()
            VStack(spacing: 0) {
                messageList
                inputBar
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(FCTheme.background, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    showResetConfirm = true
                } label: {
                    Image(systemName: "arrow.counterclockwise")
                }
            }
        }
        .confirmationDialog("Reset this conversation?", isPresented: $showResetConfirm, titleVisibility: .visible) {
            Button("Reset", role: .destructive) { Task { await resetConversation() } }
            Button("Cancel", role: .cancel) {}
        }
        .fileImporter(isPresented: $showFileImporter, allowedContentTypes: [.item], allowsMultipleSelection: false) { result in
            Task { await handleFileImport(result) }
        }
        .task { await load() }
    }

    // MARK: - Message list

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if isLoading {
                        ProgressView().tint(FCTheme.accent).padding(.top, 40)
                    } else if let loadError {
                        ErrorStateView(message: loadError) { Task { await load() } }
                            .padding(.top, 40)
                    } else if items.isEmpty && !isStreamingText && !isThinking {
                        emptyState
                    }

                    ForEach(items) { item in
                        ChatItemView(item: item).id(item.id)
                    }
                    if isThinking {
                        ThinkingBubble().id("thinking")
                    }
                    if isStreamingText {
                        MessageBubble(role: .agent, text: streamingText).id("streaming")
                    }
                    if isToolRunning, let liveToolName {
                        ToolLiveIndicator(name: liveToolName).id("tool-live")
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 16)
            }
            .onChange(of: items.count) { _ in scrollToBottom(proxy) }
            .onChange(of: streamingText) { _ in scrollToBottom(proxy) }
            .onChange(of: isThinking) { _ in scrollToBottom(proxy) }
            .onChange(of: isToolRunning) { _ in scrollToBottom(proxy) }
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.2)) {
            proxy.scrollTo("bottom", anchor: .bottom)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            Image(systemName: "bolt.horizontal.circle")
                .font(.system(size: 40))
                .foregroundStyle(FCTheme.muted.opacity(0.5))
            Text("FREECLAW")
                .font(.system(.footnote, design: .rounded).weight(.bold))
                .tracking(2)
                .foregroundStyle(FCTheme.muted.opacity(0.5))
            Text("search the web · run bash\ncontrol smart home · write files")
                .font(.system(.caption2, design: .monospaced))
                .multilineTextAlignment(.center)
                .foregroundStyle(FCTheme.muted.opacity(0.4))
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 80)
    }

    // MARK: - Input bar

    private var inputBar: some View {
        VStack(spacing: 8) {
            if let pendingAttachment {
                AttachmentPreview(attachment: pendingAttachment, isUploading: isUploading) {
                    self.pendingAttachment = nil
                }
            }
            HStack(alignment: .bottom, spacing: 10) {
                Button {
                    showFileImporter = true
                } label: {
                    Image(systemName: "paperclip")
                        .font(.system(size: 17))
                        .foregroundStyle(FCTheme.muted)
                        .frame(width: 40, height: 40)
                        .background(FCTheme.surface)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(FCTheme.border, lineWidth: 1))
                }

                TextField("Ask anything…", text: $inputText, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($inputFocused)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
                    .background(FCTheme.surface)
                    .foregroundStyle(FCTheme.text)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(FCTheme.border, lineWidth: 1))

                Button {
                    Task { await send() }
                } label: {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(FCTheme.background)
                        .frame(width: 40, height: 40)
                        .background(canSend ? FCTheme.accent : Color(hex: 0x2A2A30))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .disabled(!canSend)
            }
            if let sendError {
                Text(sendError)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(FCTheme.danger)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(FCTheme.background.opacity(0.97))
        .overlay(Rectangle().frame(height: 1).foregroundStyle(FCTheme.border), alignment: .top)
    }

    // MARK: - Networking

    private func load() async {
        isLoading = true
        loadError = nil
        do {
            try await store.client.openChat(user: user, conversationId: conversationId)
            let active = try await store.client.fetchActiveConversation()
            if let activeTitle = active.title, !activeTitle.isEmpty {
                title = activeTitle
            }
            items = buildDisplayItems(from: active.messages)
        } catch {
            if case FreeClawError.unauthorized = error {
                store.sessionExpired()
            } else {
                loadError = error.localizedDescription
            }
        }
        isLoading = false
    }

    private func send() async {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || pendingAttachment != nil else { return }

        var messageToSend = text
        if let attachment = pendingAttachment {
            let tag = "[File uploaded: \"\(attachment.filename)\" \u{2192} \(attachment.path)]"
            messageToSend = text.isEmpty ? tag : "\(tag)\n\n\(text)"
        }

        inputText = ""
        inputFocused = false
        pendingAttachment = nil
        sendError = nil
        isSending = true

        items.append(.user(id: "local-\(UUID().uuidString)", text: text))
        isThinking = true

        do {
            for try await event in store.client.sendMessage(messageToSend) {
                apply(event)
            }
        } catch {
            isThinking = false
            isStreamingText = false
            isToolRunning = false
            if case FreeClawError.unauthorized = error {
                store.sessionExpired()
            } else {
                sendError = error.localizedDescription
            }
        }

        isSending = false
    }

    private func apply(_ event: ChatStreamEvent) {
        switch event {
        case .token(let piece):
            isThinking = false
            isToolRunning = false
            isStreamingText = true
            streamingText += piece
        case .toolCall(let name):
            isStreamingText = false
            streamingText = ""
            isThinking = false
            isToolRunning = true
            liveToolName = name
        case .toolResult:
            isToolRunning = false
            liveToolName = nil
            isThinking = true
        case .error(let message):
            isThinking = false
            isStreamingText = false
            streamingText = ""
            isToolRunning = false
            sendError = message
        case .done(let conversation):
            isThinking = false
            isStreamingText = false
            streamingText = ""
            isToolRunning = false
            liveToolName = nil
            if !conversation.isEmpty {
                items = buildDisplayItems(from: conversation)
            }
        }
    }

    private func resetConversation() async {
        do {
            try await store.client.resetConversation()
            items = []
            streamingText = ""
            isStreamingText = false
            isThinking = false
            isToolRunning = false
        } catch {
            sendError = error.localizedDescription
        }
    }

    private func handleFileImport(_ result: Result<[URL], Error>) async {
        guard case .success(let urls) = result, let url = urls.first else { return }
        isUploading = true

        let accessed = url.startAccessingSecurityScopedResource()
        defer {
            if accessed { url.stopAccessingSecurityScopedResource() }
            isUploading = false
        }

        do {
            let data = try Data(contentsOf: url)
            let filename = url.lastPathComponent
            let mime = mimeType(for: url)
            let path = try await store.client.uploadFile(data: data, filename: filename, mimeType: mime)
            pendingAttachment = PendingAttachment(filename: filename, path: path, isImage: mime.hasPrefix("image/"))
        } catch {
            sendError = "Upload failed: \(error.localizedDescription)"
        }
    }

    private func mimeType(for url: URL) -> String {
        if let type = UTType(filenameExtension: url.pathExtension), let mime = type.preferredMIMEType {
            return mime
        }
        return "application/octet-stream"
    }
}
