import SwiftUI

enum BubbleRole { case user, agent }

/// A single chat bubble. Used both for finished messages (from
/// buildDisplayItems) and, with `.agent`, the live-streaming reply in
/// progress. `imagePath` is a server-relative path (e.g.
/// "static/Elliot/conversations/<id>/<file>.jpg") resolved against the
/// current server's base URL — the request rides on the same shared cookie
/// jar as the rest of the app, so the authenticated /static/ route just works.
struct MessageBubble: View {
    @EnvironmentObject private var store: ServerStore
    var role: BubbleRole
    var text: String
    var imagePath: String? = nil

    @State private var isShowingFullImage = false

    var body: some View {
        VStack(alignment: role == .user ? .trailing : .leading, spacing: 4) {
            Text(role == .user ? "YOU" : "AGENT")
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(role == .user ? FCTheme.accentDim : FCTheme.muted)

            VStack(alignment: .leading, spacing: text.isEmpty ? 0 : 8) {
                if let imagePath, let url = imageURL(for: imagePath) {
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .empty:
                            imagePlaceholder { ProgressView().tint(FCTheme.accent) }
                        case .success(let image):
                            image
                                .resizable()
                                .scaledToFit()
                                .frame(maxWidth: 240, maxHeight: 300)
                                .clipShape(RoundedRectangle(cornerRadius: 6))
                                .onTapGesture { isShowingFullImage = true }
                        case .failure:
                            imagePlaceholder {
                                Image(systemName: "photo.badge.exclamationmark")
                                    .foregroundStyle(FCTheme.muted)
                            }
                        @unknown default:
                            EmptyView()
                        }
                    }
                    .fullScreenCover(isPresented: $isShowingFullImage) {
                        FullScreenImageView(url: url)
                    }
                }
                if !text.isEmpty {
                    MarkdownText(text)
                }
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 10)
            .background(role == .user ? FCTheme.userBubble : FCTheme.agentBubble)
            .overlay(
                RoundedRectangle(cornerRadius: 5)
                    .stroke(role == .user ? FCTheme.userBorder : FCTheme.border, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 5))
        }
        .frame(maxWidth: 320, alignment: role == .user ? .trailing : .leading)
        .frame(maxWidth: .infinity, alignment: role == .user ? .trailing : .leading)
    }

    private func imageURL(for path: String) -> URL? {
        store.client.baseURL.appendingPathComponent(path)
    }

    private func imagePlaceholder<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        RoundedRectangle(cornerRadius: 6)
            .fill(FCTheme.surface)
            .frame(width: 160, height: 160)
            .overlay(content())
    }
}

/// Full-screen, tap-to-dismiss viewer for a bubble's attached image.
private struct FullScreenImageView: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            AsyncImage(url: url) { phase in
                if case .success(let image) = phase {
                    image.resizable().scaledToFit()
                } else {
                    ProgressView().tint(.white)
                }
            }
        }
        .onTapGesture { dismiss() }
        .overlay(alignment: .topTrailing) {
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(.white, .black.opacity(0.4))
            }
            .padding()
        }
    }
}

/// Routes a display item to the right bubble/card.
struct ChatItemView: View {
    var item: ChatDisplayItem

    var body: some View {
        switch item {
        case .user(_, let text, let imagePath):
            MessageBubble(role: .user, text: text, imagePath: imagePath)
        case .agent(_, let text):
            MessageBubble(role: .agent, text: text)
        case .tool(_, let name, let argsJSON, let resultText, let isError):
            ToolCallView(name: name, argsJSON: argsJSON, resultText: resultText, isError: isError)
        }
    }
}

/// Collapsible tool call/result card — tap to expand, mirrors the
/// `<details>`-style block in chat.html's addToolBlock().
struct ToolCallView: View {
    var name: String
    var argsJSON: String
    var resultText: String
    var isError: Bool

    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                    Text("tool: \(name)")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .tracking(0.5)
                }
                .foregroundStyle(isExpanded ? FCTheme.accent : FCTheme.muted)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(Color(hex: 0x0F0F12))
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(FCTheme.border, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 4))
            }
            .buttonStyle(.plain)

            if isExpanded {
                VStack(alignment: .leading, spacing: 0) {
                    section(label: "call · \(name)", content: prettyPrinted(argsJSON), color: FCTheme.codeText)
                    Divider().overlay(FCTheme.border)
                    section(label: "result", content: resultText, color: isError ? FCTheme.danger : FCTheme.codeText)
                }
                .background(FCTheme.background)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(FCTheme.border, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 4))
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func section(label: String, content: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label.uppercased())
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(FCTheme.muted)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(content)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(color)
                    .textSelection(.enabled)
            }
        }
        .padding(10)
    }

    private func prettyPrinted(_ jsonString: String) -> String {
        guard let data = jsonString.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let pretty = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
              let string = String(data: pretty, encoding: .utf8)
        else {
            return jsonString
        }
        return string
    }
}

/// Small pulsing pill shown while a tool call is in flight, before its
/// result (and thus the collapsible card) exists.
struct ToolLiveIndicator: View {
    var name: String
    @State private var pulse = false

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(FCTheme.accent)
                .frame(width: 6, height: 6)
                .opacity(pulse ? 0.3 : 1)
                .animation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true), value: pulse)
            Text("running tool: \(name)")
                .font(.system(size: 10, design: .monospaced))
        }
        .foregroundStyle(FCTheme.muted)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color(hex: 0x0F0F12))
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(FCTheme.border, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear { pulse = true }
    }
}

/// Three-dot "thinking" indicator shown between a user message and the
/// first streamed token / tool call.
struct ThinkingBubble: View {
    @State private var animate = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(FCTheme.muted)
                    .frame(width: 6, height: 6)
                    .scaleEffect(animate ? 1 : 0.7)
                    .opacity(animate ? 1 : 0.3)
                    .animation(
                        .easeInOut(duration: 0.7).repeatForever(autoreverses: true).delay(Double(i) * 0.15),
                        value: animate
                    )
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(FCTheme.agentBubble)
        .overlay(RoundedRectangle(cornerRadius: 5).stroke(FCTheme.border, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 5))
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear { animate = true }
    }
}

/// Pill shown above the input bar while a file is attached/uploading.
struct AttachmentPreview: View {
    @EnvironmentObject private var store: ServerStore
    var attachment: PendingAttachment
    var isUploading: Bool
    var onRemove: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            if attachment.isImage {
                AsyncImage(url: store.client.baseURL.appendingPathComponent(attachment.path)) { phase in
                    if case .success(let image) = phase {
                        image.resizable().scaledToFill()
                    } else {
                        Image(systemName: "photo").foregroundStyle(FCTheme.accent)
                    }
                }
                .frame(width: 28, height: 28)
                .clipShape(RoundedRectangle(cornerRadius: 4))
            } else {
                Image(systemName: "doc")
                    .foregroundStyle(FCTheme.accent)
            }
            Text(attachment.filename)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(Color(hex: 0xD8F0A0))
                .lineLimit(1)
            Spacer()
            Text(isUploading ? "uploading…" : "ready")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(FCTheme.muted)
            Button(action: onRemove) {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(FCTheme.muted)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(FCTheme.userBubble)
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(FCTheme.userBorder, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}
