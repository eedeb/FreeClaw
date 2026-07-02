import SwiftUI
import WidgetKit

struct FreeClawWidgetEntryView: View {
    var entry: FreeClawWidgetProvider.Entry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: "mic.fill")
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(.tint)
            Spacer(minLength: 0)
            Text(entry.userName)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Text(entry.conversationTitle)
                .font(.subheadline.weight(.semibold))
                .lineLimit(2)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .padding()
        .widgetURL(entry.deepLinkURL)
        .containerBackground(.black, for: .widget)
    }
}
