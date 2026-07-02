import Foundation

/// Builds a FreeClawClient from the server address the main app saved to
/// the shared App Group UserDefaults suite. Auth relies entirely on the
/// cookie FreeClawClient reads from the shared App Group cookie storage —
/// there is no separate widget login; if the session has expired, the
/// caller just gets an empty result and the user opens the app once to
/// re-authenticate before reconfiguring the widget.
enum WidgetServerAccess {
    private static let addressKey = "fc.server.address"
    private static let httpsKey = "fc.server.useHTTPS"
    private static let suiteName = "group.dev.eedeb.freeclaw"

    static func makeClient(timeoutInterval: TimeInterval = 5) -> FreeClawClient? {
        guard let defaults = UserDefaults(suiteName: suiteName),
              let address = defaults.string(forKey: addressKey), !address.isEmpty
        else { return nil }
        let config = ServerConfig(address: address, useHTTPS: defaults.bool(forKey: httpsKey))
        let client = FreeClawClient(timeoutInterval: timeoutInterval)
        client.configure(baseURL: config.baseURL)
        return client
    }
}
