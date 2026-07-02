import SwiftUI

/// FreeClaw's dark, lime-accented palette — mirrors the web UI's CSS variables
/// (Flask/templates/index.html, chat.html) so the native app feels like the same product.
enum FCTheme {
    static let background   = Color(hex: 0x0D0D0F)
    static let surface      = Color(hex: 0x141416)
    static let border       = Color(hex: 0x222228)
    static let accent       = Color(hex: 0xC8F04A)
    static let accentDim    = Color(hex: 0xA8CC38)
    static let text         = Color(hex: 0xE8E8EC)
    static let muted        = Color(hex: 0x666672)
    static let userBubble   = Color(hex: 0x1A1F10)
    static let userBorder   = Color(hex: 0x2A3318)
    static let agentBubble  = Color(hex: 0x141416)
    static let codeBg       = Color(hex: 0x0A0A0C)
    static let codeText     = Color(hex: 0xC8E6A0)
    static let danger       = Color(hex: 0xE05555)
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        let r = Double((hex >> 16) & 0xFF) / 255
        let g = Double((hex >> 8) & 0xFF) / 255
        let b = Double(hex & 0xFF) / 255
        self.init(.sRGB, red: r, green: g, blue: b, opacity: opacity)
    }
}
