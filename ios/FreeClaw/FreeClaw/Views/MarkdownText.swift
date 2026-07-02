import SwiftUI

/// Dependency-free markdown rendering: splits ``` fenced code blocks out
/// into their own monospaced panels, and runs everything else through
/// AttributedString's built-in inline markdown (bold, italics, links,
/// inline code). Not a full CommonMark renderer — no tables or nested
/// lists — but it covers what the agent actually sends.
struct MarkdownText: View {
    private let segments: [Segment]

    init(_ raw: String) {
        segments = MarkdownText.parse(raw)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(segments) { segment in
                switch segment.kind {
                case .text(let attributed):
                    Text(attributed)
                        .foregroundStyle(FCTheme.text)
                        .textSelection(.enabled)
                case .code(let code):
                    ScrollView(.horizontal, showsIndicators: false) {
                        Text(code)
                            .font(.system(.footnote, design: .monospaced))
                            .foregroundStyle(FCTheme.codeText)
                            .textSelection(.enabled)
                    }
                    .padding(10)
                    .background(FCTheme.codeBg)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(FCTheme.border, lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
                }
            }
        }
    }

    private struct Segment: Identifiable {
        let id = UUID()
        let kind: Kind
        enum Kind {
            case text(AttributedString)
            case code(String)
        }
    }

    private static func parse(_ raw: String) -> [Segment] {
        guard !raw.isEmpty else { return [] }
        var result: [Segment] = []
        let parts = raw.components(separatedBy: "```")

        for (index, part) in parts.enumerated() {
            if index % 2 == 1 {
                result.append(contentsOf: codeSegment(from: part))
            } else {
                guard !part.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
                let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
                let attributed = (try? AttributedString(markdown: part, options: options)) ?? AttributedString(part)
                result.append(Segment(kind: .text(attributed)))
            }
        }

        if result.isEmpty {
            result.append(Segment(kind: .text(AttributedString(raw))))
        }
        return result
    }

    private static func codeSegment(from part: String) -> [Segment] {
        var code = part
        // Drop a leading language tag (```swift\n...) if present.
        if let newline = code.firstIndex(of: "\n") {
            let firstLine = code[code.startIndex..<newline]
            if !firstLine.contains(" "), firstLine.count < 20 {
                code = String(code[code.index(after: newline)...])
            }
        }
        let trimmed = code.trimmingCharacters(in: .newlines)
        guard !trimmed.isEmpty else { return [] }
        return [Segment(kind: .code(trimmed))]
    }
}
