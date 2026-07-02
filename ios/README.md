# FreeClaw for iOS

A native SwiftUI client for [FreeClaw](../README.md). It's not a wrapper
around the web UI — it talks directly to the Flask server's REST + SSE API
and renders its own chat, user, and conversation screens.

## Requirements

- Xcode 15 or later, on macOS
- A running FreeClaw agent reachable on your network (`http://<ip>:6767`)

## Opening the project

1. Copy or clone this repo onto a Mac (this app was written from a Windows
   dev machine, so it's never been opened in Xcode — do that first before
   relying on it).
2. Open `ios/FreeClaw/FreeClaw.xcodeproj` in Xcode.
3. Select the `FreeClaw` target → **Signing & Capabilities** → set your own
   Team so Xcode can code-sign it for a device (the bundle identifier
   `dev.eedeb.freeclaw` is a placeholder; change it if it collides with
   something already provisioned on your account).
4. Pick a simulator or your device and hit Run.

## Using the app

On first launch you'll be asked to connect, the same way the Home Assistant
app asks for a server: enter the agent's address (e.g. `192.168.1.42:6767`,
exactly what `install.sh` prints) and the password you set for the web UI.
From there:

- **Users** — FreeClaw supports multiple independent users per agent; this
  is the home screen. Swipe to delete, tap **+** to add one.
- **Chats** — tap a user to see their conversations; swipe to delete, tap
  the compose icon for a new chat.
- **Chat** — streams responses token-by-token exactly like the web UI,
  shows tool calls as collapsible cards you can tap open, supports
  attaching a file (including images) via the paperclip button, and has a
  reset button in the toolbar.
- **Settings** (gear icon on the home screen) — log out, or forget the
  server entirely to connect to a different one.

The server address and password are stored in Keychain/UserDefaults on
device; the app re-authenticates automatically on launch and drops back to
a "log in again" screen if the session ever expires server-side.

## Notes / scope

- Talks to plain HTTP by default (matching how `install.sh` sets things
  up), so `Info.plist` allows arbitrary local loads. Toggle "Use HTTPS" on
  the connect screen if you've put the agent behind TLS.
- Markdown rendering covers bold/italic/inline code/links and fenced code
  blocks — not full CommonMark (no tables, no nested lists).
- The web UI's `.env` / API-key settings panel (Groq/NVIDIA/OpenRouter
  keys, HA integration, OpenAI-compatible API toggle) isn't exposed here;
  that's server administration, not something you want editable from a
  phone. Everything user-facing — chat, users, conversations — is native.
- No app icon is bundled yet; only `AccentColor` is defined in
  `Assets.xcassets`. Drop your own icon images into a new `AppIcon`
  image set before shipping to TestFlight/App Store.
