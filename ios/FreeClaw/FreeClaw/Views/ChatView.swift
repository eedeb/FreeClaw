import SwiftUI
import PhotosUI
import UIKit
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
    @State private var photoPickerItem: PhotosPickerItem?
    @State private var showResetConfirm = false

    @StateObject private var speech = SpeechCoordinator()
    @State private var isVoiceModeEnabled = false

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
                if isVoiceModeEnabled {
                    voiceInputBar
                } else {
                    inputBar
                }
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(FCTheme.background, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    toggleVoiceMode()
                } label: {
                    Image(systemName: isVoiceModeEnabled ? "waveform.circle.fill" : "waveform.circle")
                }
                .disabled(isSending)
            }
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
        .onChange(of: photoPickerItem) { _ in
            Task { await handlePhotoPickerSelection() }
        }
        .task { await load() }
        .onDisappear { speech.deactivateSession() }
    }

    private func toggleVoiceMode() {
        if isVoiceModeEnabled {
            isVoiceModeEnabled = false
            speech.deactivateSession()
        } else {
            Task {
                if await speech.requestPermissions() {
                    isVoiceModeEnabled = true
                }
            }
        }
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
            Image("EagleLogo")
                .resizable()
                .scaledToFit()
                .frame(width: 56, height: 56)
                .opacity(0.5)
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
                PhotosPicker(selection: $photoPickerItem, matching: .images) {
                    Image(systemName: "photo")
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

    // MARK: - Voice input bar

    private var voiceInputBar: some View {
        VStack(spacing: 10) {
            if speech.permissionState == .denied {
                permissionDeniedBanner
            } else {
                if speech.isRecording, !speech.liveTranscript.isEmpty {
                    Text(speech.liveTranscript)
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundStyle(FCTheme.text)
                        .multilineTextAlignment(.center)
                        .lineLimit(3)
                        .padding(.horizontal, 24)
                }

                pushToTalkButton

                Text(voiceStatusLabel)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(FCTheme.muted)

                if let speechError = speech.errorMessage {
                    Text(speechError)
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(FCTheme.danger)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
            }
            if let sendError {
                Text(sendError)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(FCTheme.danger)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 16)
        .frame(maxWidth: .infinity)
        .background(FCTheme.background.opacity(0.97))
        .overlay(Rectangle().frame(height: 1).foregroundStyle(FCTheme.border), alignment: .top)
    }

    private var voiceStatusLabel: String {
        if isSending { return "Thinking…" }
        if speech.isRecording { return "Listening — release to send" }
        if speech.isSpeaking { return "Speaking…" }
        return "Hold to talk"
    }

    private var pushToTalkButton: some View {
        Circle()
            .fill(speech.isRecording ? FCTheme.accent : FCTheme.surface)
            .frame(width: 76, height: 76)
            .overlay(
                Image(systemName: speech.isRecording ? "mic.fill" : "mic")
                    .font(.system(size: 26))
                    .foregroundStyle(speech.isRecording ? FCTheme.background : FCTheme.accent)
            )
            .overlay(Circle().stroke(FCTheme.accent.opacity(speech.isRecording ? 0 : 0.3), lineWidth: 1.5))
            .scaleEffect(speech.isRecording ? 1.08 : 1.0)
            .animation(.easeInOut(duration: 0.15), value: speech.isRecording)
            .opacity(isSending ? 0.4 : 1)
            .contentShape(Circle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard !isSending, !speech.isRecording else { return }
                        speech.startRecording()
                    }
                    .onEnded { _ in
                        guard speech.isRecording else { return }
                        Task { await finishVoiceTurn() }
                    }
            )
    }

    private var permissionDeniedBanner: some View {
        VStack(spacing: 8) {
            Text("Voice mode needs microphone and speech-recognition access.")
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(FCTheme.muted)
                .multilineTextAlignment(.center)
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
            .font(.system(.footnote, design: .monospaced).weight(.semibold))
            .foregroundStyle(FCTheme.accent)
        }
        .padding(.horizontal, 24)
    }

    private func finishVoiceTurn() async {
        guard let transcript = await speech.stopRecordingAndGetTranscript() else { return }
        await send(overrideText: transcript)
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

    private func send(overrideText: String? = nil) async {
        let text = (overrideText ?? inputText).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || pendingAttachment != nil else { return }

        var messageToSend = text
        let attachmentImagePath = pendingAttachment?.isImage == true ? pendingAttachment?.path : nil
        if let attachment = pendingAttachment {
            let tag = "[File uploaded: \"\(attachment.filename)\" \u{2192} \(attachment.path)]"
            messageToSend = text.isEmpty ? tag : "\(tag)\n\n\(text)"
        }

        inputText = ""
        inputFocused = false
        pendingAttachment = nil
        sendError = nil
        isSending = true

        items.append(.user(id: "local-\(UUID().uuidString)", text: text, imagePath: attachmentImagePath))
        isThinking = true

        do {
            for try await event in store.client.sendMessage(messageToSend) {
                apply(event)
            }
        } catch {
            isThinking = false
            isStreamingText = false
            isToolRunning = false
            if isVoiceModeEnabled { speech.discardPendingReply() }
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
            if isVoiceModeEnabled { speech.appendReplyToken(piece) }
        case .toolCall(let name, let url):
            isStreamingText = false
            streamingText = ""
            isThinking = false
            isToolRunning = true
            liveToolName = name
            // Tool calls are never spoken — only shown on screen via the
            // live indicator/collapsible card below — and any reply text
            // buffered before this point belongs to a bubble the chat UI
            // itself discards, so drop it rather than speak it.
            if isVoiceModeEnabled { speech.discardPendingReply() }
            // open_webpage/open_app run entirely client-side: the backend
            // can't reach the user's device, so it just reports the url
            // and leaves the actual opening to us. UIApplication.open
            // launches Safari for http(s) links and the matching app for
            // a custom URI scheme.
            if (name == "open_webpage" || name == "open_app"), let url, let target = URL(string: url) {
                UIApplication.shared.open(target)
            }
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
            if isVoiceModeEnabled { speech.discardPendingReply() }
        case .done(let conversation):
            isThinking = false
            isStreamingText = false
            streamingText = ""
            isToolRunning = false
            liveToolName = nil
            if !conversation.isEmpty {
                items = buildDisplayItems(from: conversation)
            }
            if isVoiceModeEnabled { speech.finishReply() }
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

    private func handlePhotoPickerSelection() async {
        guard let item = photoPickerItem else { return }
        isUploading = true
        defer {
            isUploading = false
            photoPickerItem = nil
        }

        do {
            guard let data = try await item.loadTransferable(type: Data.self) else {
                sendError = "Couldn't load the selected photo."
                return
            }
            let type = item.supportedContentTypes.first ?? .jpeg
            let ext = type.preferredFilenameExtension ?? "jpg"
            let mime = type.preferredMIMEType ?? "image/jpeg"
            let filename = "photo-\(Int(Date().timeIntervalSince1970)).\(ext)"
            let path = try await store.client.uploadFile(data: data, filename: filename, mimeType: mime)
            pendingAttachment = PendingAttachment(filename: filename, path: path, isImage: true)
        } catch {
            sendError = "Upload failed: \(error.localizedDescription)"
        }
    }
}
