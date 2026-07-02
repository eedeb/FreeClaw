import Foundation

/// A remembered FreeClaw agent — just an address, the way the Home Assistant
/// app remembers a server URL instead of a discrete host/port pair.
struct ServerConfig: Codable, Equatable {
    var address: String
    var useHTTPS: Bool

    var baseURL: URL {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let lowered = trimmed.lowercased()
        if lowered.hasPrefix("http://") || lowered.hasPrefix("https://") {
            return URL(string: trimmed) ?? ServerConfig.fallback
        }
        let scheme = useHTTPS ? "https" : "http"
        return URL(string: "\(scheme)://\(trimmed)") ?? ServerConfig.fallback
    }

    var displayHost: String {
        guard let host = baseURL.host else { return address }
        if let port = baseURL.port {
            return "\(host):\(port)"
        }
        return host
    }

    private static let fallback = URL(string: "http://localhost")!
}
