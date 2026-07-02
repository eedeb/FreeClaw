import SwiftUI

/// First-run onboarding — enter the agent's address and password, the same
/// shape as the Home Assistant app's "connect to your server" screen.
struct ConnectView: View {
    @EnvironmentObject private var store: ServerStore
    @State private var address = ""
    @State private var password = ""
    @State private var useHTTPS = false
    @State private var isConnecting = false
    @FocusState private var focusedField: Field?

    private enum Field { case address, password }

    private var canConnect: Bool {
        !address.trimmingCharacters(in: .whitespaces).isEmpty && !password.isEmpty
    }

    var body: some View {
        ZStack {
            FCTheme.background.ignoresSafeArea()
            ScrollView {
                VStack(spacing: 28) {
                    header
                    form
                    FCPrimaryButton(title: "Connect", isLoading: isConnecting) {
                        Task { await connect() }
                    }
                    .disabled(!canConnect || isConnecting)
                    .opacity(canConnect ? 1 : 0.5)

                    if let error = store.connectionError {
                        Text(error)
                            .font(.system(.footnote, design: .monospaced))
                            .foregroundStyle(FCTheme.danger)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 24)
                    }
                }
                .padding(.top, 72)
                .padding(.horizontal, 24)
                .padding(.bottom, 40)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        VStack(spacing: 12) {
            RoundedRectangle(cornerRadius: 20)
                .fill(FCTheme.surface)
                .frame(width: 72, height: 72)
                .overlay(
                    Image(systemName: "bolt.horizontal.circle.fill")
                        .font(.system(size: 32))
                        .foregroundStyle(FCTheme.accent)
                )
                .overlay(RoundedRectangle(cornerRadius: 20).stroke(FCTheme.accent.opacity(0.25), lineWidth: 1))
            Text("FREECLAW")
                .font(.system(.title3, design: .rounded).weight(.bold))
                .tracking(3)
                .foregroundStyle(FCTheme.text)
            Text("Connect to your agent")
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(FCTheme.muted)
        }
    }

    private var form: some View {
        VStack(spacing: 12) {
            FCField(label: "Server Address", placeholder: "192.168.1.42:6767", text: $address, keyboard: .URL)
                .focused($focusedField, equals: .address)
                .submitLabel(.next)
                .onSubmit { focusedField = .password }

            FCSecureField(label: "Password", placeholder: "Agent password", text: $password)
                .focused($focusedField, equals: .password)
                .submitLabel(.go)
                .onSubmit { Task { await connect() } }

            Toggle(isOn: $useHTTPS) {
                Text("Use HTTPS")
                    .font(.system(.footnote, design: .monospaced))
                    .foregroundStyle(FCTheme.muted)
            }
            .tint(FCTheme.accent)
            .padding(.top, 2)

            Text("Find this in the FreeClaw install output — usually your machine's local IP and port 6767.")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(FCTheme.muted.opacity(0.7))
                .padding(.top, 2)
        }
    }

    private func connect() async {
        guard canConnect else { return }
        focusedField = nil
        isConnecting = true
        _ = await store.connect(address: address, useHTTPS: useHTTPS, password: password)
        isConnecting = false
    }
}
