import Foundation

/// A user + conversation the widget (or any other deep link) wants the app
/// to open directly into, with voice mode already active.
struct PendingVoiceTarget: Equatable {
    let user: String
    let conversationId: String
}

/// App-wide connection state: which server we're pointed at, whether we're
/// currently authenticated against it, and the shared API client. Lives for
/// the lifetime of the app as an environment object.
@MainActor
final class ServerStore: ObservableObject {
    @Published private(set) var config: ServerConfig?
    @Published private(set) var isAuthenticated = false
    @Published var connectionError: String?
    @Published var pendingVoiceTarget: PendingVoiceTarget?

    let client = FreeClawClient()

    private let addressKey = "fc.server.address"
    private let httpsKey = "fc.server.useHTTPS"
    private let passwordKey = "fc.server.password"

    // Shared with the widget extension via the App Group so it can read the
    // server address without the user having to configure it twice. Falls
    // back to `.standard` if the App Group entitlement isn't set up yet,
    // rather than crashing.
    private let defaults = UserDefaults(suiteName: "group.dev.eedeb.freeclaw") ?? .standard

    init() {
        if let address = defaults.string(forKey: addressKey), !address.isEmpty {
            let cfg = ServerConfig(address: address, useHTTPS: defaults.bool(forKey: httpsKey))
            config = cfg
            client.configure(baseURL: cfg.baseURL)
        }
    }

    var savedPassword: String? {
        KeychainStore.get(passwordKey)
    }

    /// Used by the connect screen: tries a fresh address/password pair and,
    /// on success, remembers it for next launch.
    func connect(address: String, useHTTPS: Bool, password: String) async -> Bool {
        let cfg = ServerConfig(address: address, useHTTPS: useHTTPS)
        client.configure(baseURL: cfg.baseURL)
        connectionError = nil
        do {
            guard try await client.login(password: password) else {
                connectionError = "Incorrect password."
                return false
            }
            config = cfg
            defaults.set(address, forKey: addressKey)
            defaults.set(useHTTPS, forKey: httpsKey)
            KeychainStore.set(password, for: passwordKey)
            isAuthenticated = true
            return true
        } catch {
            connectionError = "Couldn't reach \(cfg.displayHost): \(error.localizedDescription)"
            return false
        }
    }

    /// Used at launch when a server + password are already remembered.
    func reconnectWithSavedCredentials() async {
        guard let config, let password = savedPassword else { return }
        client.configure(baseURL: config.baseURL)
        connectionError = nil
        do {
            let ok = try await client.login(password: password)
            isAuthenticated = ok
            if !ok { connectionError = "Saved password was rejected." }
        } catch {
            connectionError = "Couldn't reach \(config.displayHost): \(error.localizedDescription)"
        }
    }

    /// Call when any API request comes back 401 mid-session (password
    /// changed on the server, session cookie expired, etc.) to drop back to
    /// the reauth screen without forgetting the server address.
    func sessionExpired() {
        isAuthenticated = false
    }

    func logout() {
        isAuthenticated = false
        Task { await client.logout() }
    }

    func forgetServer() {
        defaults.removeObject(forKey: addressKey)
        defaults.removeObject(forKey: httpsKey)
        KeychainStore.delete(passwordKey)
        client.clearSession()
        config = nil
        isAuthenticated = false
    }
}
