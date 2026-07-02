import SwiftUI

/// Top-level router: no server saved → onboarding, server saved but not
/// authenticated → reauth, otherwise the main user/chat picker.
struct RootView: View {
    @EnvironmentObject private var store: ServerStore
    @State private var isBootstrapping = true

    var body: some View {
        Group {
            if isBootstrapping {
                loadingView
            } else if store.config == nil {
                ConnectView()
            } else if !store.isAuthenticated {
                ReauthView()
            } else {
                UsersListView()
            }
        }
        .task {
            if store.config != nil, store.savedPassword != nil {
                await store.reconnectWithSavedCredentials()
            }
            isBootstrapping = false
        }
    }

    private var loadingView: some View {
        ZStack {
            FCTheme.background.ignoresSafeArea()
            ProgressView().tint(FCTheme.accent)
        }
    }
}
