import SwiftUI

/// Shown when a server is remembered but the session cookie is gone (fresh
/// install, expired session, or the saved password was rejected).
struct ReauthView: View {
    @EnvironmentObject private var store: ServerStore
    @State private var password = ""
    @State private var isConnecting = false

    var body: some View {
        ZStack {
            FCTheme.background.ignoresSafeArea()
            VStack(spacing: 24) {
                VStack(spacing: 6) {
                    Image(systemName: "lock.circle")
                        .font(.system(size: 40))
                        .foregroundStyle(FCTheme.accent)
                    Text(store.config?.displayHost ?? "Server")
                        .font(.system(.headline, design: .monospaced))
                        .foregroundStyle(FCTheme.text)
                    Text("Session expired — enter your password again")
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundStyle(FCTheme.muted)
                        .multilineTextAlignment(.center)
                }

                FCSecureField(label: "Password", placeholder: "Agent password", text: $password)
                    .onSubmit { Task { await retry() } }

                FCPrimaryButton(title: "Log In", isLoading: isConnecting) {
                    Task { await retry() }
                }
                .disabled(password.isEmpty || isConnecting)
                .opacity(password.isEmpty ? 0.5 : 1)

                Button("Use a different server") {
                    store.forgetServer()
                }
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(FCTheme.muted)

                if let error = store.connectionError {
                    Text(error)
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundStyle(FCTheme.danger)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(24)
        }
        .preferredColorScheme(.dark)
    }

    private func retry() async {
        guard let config = store.config, !password.isEmpty else { return }
        isConnecting = true
        _ = await store.connect(address: config.address, useHTTPS: config.useHTTPS, password: password)
        isConnecting = false
    }
}
