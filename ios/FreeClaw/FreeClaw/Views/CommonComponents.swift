import SwiftUI

/// Labeled text field styled to match the app's dark, monospace-accented look.
struct FCField: View {
    var label: String
    var placeholder: String
    @Binding var text: String
    var keyboard: UIKeyboardType = .default

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(FCTheme.muted)
            TextField(placeholder, text: $text)
                .keyboardType(keyboard)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(12)
                .background(FCTheme.surface)
                .foregroundStyle(FCTheme.text)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(FCTheme.border, lineWidth: 1))
        }
    }
}

/// Same styling as FCField, but with a reveal toggle for passwords.
struct FCSecureField: View {
    var label: String
    var placeholder: String
    @Binding var text: String
    @State private var isRevealed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(FCTheme.muted)
            HStack {
                Group {
                    if isRevealed {
                        TextField(placeholder, text: $text)
                    } else {
                        SecureField(placeholder, text: $text)
                    }
                }
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .foregroundStyle(FCTheme.text)

                Button {
                    isRevealed.toggle()
                } label: {
                    Image(systemName: isRevealed ? "eye.slash" : "eye")
                        .foregroundStyle(FCTheme.muted)
                }
            }
            .padding(12)
            .background(FCTheme.surface)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(FCTheme.border, lineWidth: 1))
        }
    }
}

/// Full-bleed error state with a retry action, used by any list screen that
/// failed to load from the server.
struct ErrorStateView: View {
    var message: String
    var retry: () -> Void

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 34))
                .foregroundStyle(FCTheme.danger)
            Text(message)
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(FCTheme.muted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Button("Retry", action: retry)
                .font(.system(.footnote, design: .monospaced).weight(.semibold))
                .foregroundStyle(FCTheme.accent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Primary call-to-action button (accent-filled, dark text).
struct FCPrimaryButton: View {
    var title: String
    var isLoading: Bool = false
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack {
                if isLoading { ProgressView().tint(FCTheme.background) }
                Text(isLoading ? "\(title)…" : title).fontWeight(.bold)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
        }
        .background(FCTheme.accent)
        .foregroundStyle(FCTheme.background)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
