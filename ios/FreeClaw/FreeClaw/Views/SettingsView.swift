import SwiftUI

/// Server/session management — the mobile equivalent of the web UI's
/// "log out" link and, one level further, forgetting the server entirely.
struct SettingsView: View {
    @EnvironmentObject private var store: ServerStore
    @State private var showForgetConfirm = false

    var body: some View {
        Form {
            Section("Server") {
                LabeledContent("Address", value: store.config?.displayHost ?? "—")
                LabeledContent("Connection", value: (store.config?.useHTTPS ?? false) ? "HTTPS" : "HTTP")
            }

            Section {
                Button("Log Out", role: .destructive) {
                    store.logout()
                }
            }

            Section {
                Button("Forget This Server", role: .destructive) {
                    showForgetConfirm = true
                }
            } footer: {
                Text("Removes the saved address and password from this device.")
            }
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .scrollContentBackground(.hidden)
        .background(FCTheme.background)
        .confirmationDialog("Forget this server?", isPresented: $showForgetConfirm, titleVisibility: .visible) {
            Button("Forget Server", role: .destructive) { store.forgetServer() }
            Button("Cancel", role: .cancel) {}
        }
    }
}
