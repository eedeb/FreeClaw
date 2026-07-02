import SwiftUI

@main
struct FreeClawApp: App {
    @StateObject private var store = ServerStore()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .tint(FCTheme.accent)
                .preferredColorScheme(.dark)
        }
    }
}
