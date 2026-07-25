#!/bin/bash
# pipefail matters here: build/clone output is piped through indent, and
# without it a failed `docker compose build` would be masked by sed's exit
# status and the script would sail on to report success.
set -eo pipefail

# ─────────────────────────────────────────────
#  FreeClaw — macOS Installer (Docker)
#  github.com/eedeb/FreeClaw
#
#  The Linux installer builds a venv and registers a systemd service, neither
#  of which exists on macOS. This one runs the same app in a container instead.
# ─────────────────────────────────────────────

# Colors & styles
RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"

GREEN="\033[0;32m"
LIME="\033[38;5;154m"       # #c8f04a-ish (256-color lime)
WHITE="\033[0;97m"
GRAY="\033[0;90m"
RED="\033[0;31m"
YELLOW="\033[0;33m"
CYAN="\033[0;36m"

BG_LIME="\033[48;5;154m"
BG_DARK="\033[48;5;234m"

# Top-level paths that only make sense on a native Linux install. Excluded
# from the checkout below so a Mac install doesn't carry systemd scripts it
# can never run. install.sh does the mirror image of this.
LINUX_ONLY=(
    "/install.sh"
    "/update.sh"
    "/uninstall.sh"
)

# ── Helpers ──────────────────────────────────

print_banner() {
    echo ""
    echo -e "${LIME}${BOLD}"
    echo "   ███████╗██████╗ ███████╗███████╗ ██████╗██╗      █████╗ ██╗    ██╗"
    echo "   ██╔════╝██╔══██╗██╔════╝██╔════╝██╔════╝██║     ██╔══██╗██║    ██║"
    echo "   █████╗  ██████╔╝█████╗  █████╗  ██║     ██║     ███████║██║ █╗ ██║"
    echo "   ██╔══╝  ██╔══██╗██╔══╝  ██╔══╝  ██║     ██║     ██╔══██║██║███╗██║"
    echo "   ██║     ██║  ██║███████╗███████╗╚██████╗███████╗██║  ██║╚███╔███╔╝"
    echo "   ╚═╝     ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ "
    echo -e "${RESET}"
    echo -e "   ${GRAY}An AI Agent That Doesn't Burn Your Money${RESET}"
    echo -e "   ${DIM}${GRAY}github.com/eedeb/FreeClaw  ·  macOS (Docker)${RESET}"
    echo ""
    echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"
    echo ""
}

step() {
    local num="$1"
    local msg="$2"
    echo -e "   ${BG_DARK} ${LIME}${BOLD}${num}${RESET}${BG_DARK} ${RESET} ${BOLD}${WHITE}${msg}${RESET}"
}

info()    { echo -e "     ${GRAY}→${RESET}  $1"; }
success() { echo -e "     ${LIME}✓${RESET}  $1"; }
warn()    { echo -e "     ${YELLOW}!${RESET}  $1"; }
error()   { echo -e "     ${RED}✗${RESET}  $1"; }
section_gap() { echo ""; }
divider() { echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"; }

indent() { sed 's/^/       /'; }

# ── Preflight ────────────────────────────────

print_banner

step "0" "Checking prerequisites..."
section_gap

if [[ "$(uname -s)" != "Darwin" ]]; then
    error "This is the macOS installer, but this machine is $(uname -s)."
    info "On Linux use the native installer instead:"
    info "  ${LIME}curl -fsSL https://freeclaw.eedeb.dev/install.sh | bash${RESET}"
    exit 1
fi
success "macOS detected"

if command -v git &>/dev/null; then
    success "git found"
else
    error "git is required but not found."
    info "Install Apple's command line tools with: ${LIME}xcode-select --install${RESET}"
    exit 1
fi

if command -v docker &>/dev/null; then
    success "docker found"
else
    error "Docker is required but not found."
    section_gap
    info "Install Docker Desktop, then re-run this script:"
    info "  ${LIME}https://www.docker.com/products/docker-desktop/${RESET}"
    info "or with Homebrew: ${LIME}brew install --cask docker${RESET}"
    exit 1
fi

if docker compose version &>/dev/null; then
    success "docker compose found"
else
    error "The 'docker compose' plugin (Compose v2) is required but not available."
    info "Update Docker Desktop to a current version and try again."
    exit 1
fi

if docker info &>/dev/null; then
    success "Docker daemon is running"
else
    error "Docker is installed but the daemon isn't running."
    info "Open Docker Desktop, wait for it to finish starting, then re-run this."
    exit 1
fi

section_gap
divider
section_gap

# ── Clone ────────────────────────────────────

step "1" "Cloning repository..."
section_gap
info "Fetching from github.com/eedeb/FreeClaw"

if [[ -e FreeClaw ]]; then
    error "A 'FreeClaw' directory already exists here."
    info "Move or remove it first, or run this from a different folder."
    exit 1
fi

git clone --no-checkout https://github.com/eedeb/FreeClaw 2>&1 | indent
cd FreeClaw || exit 1

# Check out everything except the Linux-only scripts. Non-cone mode is what
# allows negated patterns; it needs git 2.25+, so fall back to deleting the
# files after a normal checkout on anything older.
if git sparse-checkout init --no-cone &>/dev/null; then
    {
        echo '/*'
        for path in "${LINUX_ONLY[@]}"; do echo "!${path}"; done
    } | git sparse-checkout set --stdin
    git checkout main 2>&1 | indent
    success "Repository ready (Linux-only files skipped)"
else
    warn "git is too old for sparse-checkout — pruning after checkout instead"
    git checkout main 2>&1 | indent
    for path in "${LINUX_ONLY[@]}"; do rm -rf ".${path}"; done
    success "Repository ready (Linux-only files removed)"
fi

INSTALL_DIR=$(pwd)

section_gap
divider
section_gap

# ── Directories ──────────────────────────────

step "2" "Setting up project directories..."
section_gap
# These are bind-mounted into the container. Docker would create them as
# root-owned directories if they didn't already exist, so make them first.
mkdir -p Flask/static logs
success "Directories ready"

section_gap
divider
section_gap

# ── Configuration ────────────────────────────

step "3" "Configuration..."
section_gap
echo -e "     ${GRAY}Just a login password now — you'll add your AI provider(s)${RESET}"
echo -e "     ${GRAY}from the web UI after install (no API keys needed here).${RESET}"
section_gap

while true; do
    read -s -p "$(echo -e "     ${LIME}?${RESET}  Set the Web UI password: ")" fc_password < /dev/tty
    echo ""
    read -s -p "$(echo -e "     ${LIME}?${RESET}  Confirm password: ")" fc_password_confirm < /dev/tty
    echo ""

    if [[ -z "$fc_password" ]]; then
        warn "Password can't be empty — please try again."
        section_gap
    elif [[ "$fc_password" != "$fc_password_confirm" ]]; then
        warn "Passwords do not match — please try again."
        section_gap
    else
        break
    fi
done

secret_key=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null \
    || LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 64)

# Written under umask 077 so the password is never briefly world-readable.
# Providers and MCP servers are added afterward from the web UI, which
# rewrites this same file in place — which is why it's a bind mount and not
# baked into the image.
#
# CUSTOM_DOMAIN is seeded here rather than set in docker-compose.yml: the
# container can't see the host's LAN IP, and a value set in compose would
# override whatever the Settings UI saves here on every restart. Change it
# from Settings -> Custom Domain if you reach FreeClaw from another machine.
(
    umask 077
    {
        printf 'FC_PASSWORD=%s\n' "$fc_password"
        printf 'SECRET_KEY=%s\n' "$secret_key"
        printf 'CUSTOM_DOMAIN=%s\n' "http://localhost:6767"
    } > .env
)
success "Password saved"
success "Session secret generated"

section_gap
divider
section_gap

# ── Build & start ────────────────────────────

step "4" "Building the container image..."
section_gap
warn "First build downloads PyTorch — expect a few minutes."
section_gap

docker compose -f docker/docker-compose.yml build 2>&1 | indent
success "Image built"

section_gap
info "Starting FreeClaw..."
docker compose -f docker/docker-compose.yml up -d 2>&1 | indent
success "Container running (restarts automatically with Docker)"

section_gap
info "Waiting for the web UI to come up..."
ready=""
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "http://localhost:6767/login" 2>/dev/null; then
        ready="yes"
        break
    fi
    sleep 2
done

if [[ -n "$ready" ]]; then
    success "Web UI is responding"
else
    warn "The UI didn't respond within 2 minutes."
    info "Check the logs with:"
    info "  ${LIME}docker compose -f docker/docker-compose.yml logs -f${RESET}"
fi

section_gap
divider
section_gap

# ── CLI ──────────────────────────────────────

step "5" "Installing the freeclaw CLI..."
section_gap

cli_target="/usr/local/bin/freeclaw"
cli_body="#!/bin/bash
cd \"${INSTALL_DIR}\" || exit 1
exec docker compose -f docker/docker-compose.yml exec freeclaw python3 -m src.cli \"\$@\"
"

# Try a plain write first; fall back to sudo (which prompts on /dev/tty, so
# it still works under `curl | bash`), and finally to ~/.local/bin if the
# user would rather not give a password. Failing to install the CLI is not
# fatal — the web UI is unaffected — so this never aborts the install.
write_cli() { printf '%s' "$cli_body" > "$1" 2>/dev/null && chmod +x "$1" 2>/dev/null; }

if write_cli "$cli_target"; then
    success "CLI installed — run 'freeclaw' from anywhere"
elif info "Writing to /usr/local/bin needs administrator access" \
    && sudo mkdir -p /usr/local/bin 2>/dev/null \
    && printf '%s' "$cli_body" | sudo tee "$cli_target" > /dev/null 2>&1 \
    && sudo chmod +x "$cli_target" 2>/dev/null; then
    success "CLI installed — run 'freeclaw' from anywhere"
else
    cli_target="$HOME/.local/bin/freeclaw"
    mkdir -p "$HOME/.local/bin"
    if write_cli "$cli_target"; then
        success "CLI installed to ~/.local/bin/freeclaw"
        warn "Add ~/.local/bin to your PATH to run it by name"
    else
        warn "Could not install the freeclaw CLI — skipping (the web UI is unaffected)"
    fi
fi

chmod +x update-mac.sh uninstall-mac.sh 2>/dev/null || true

section_gap
divider
section_gap

# ── Providers ────────────────────────────────

step "6" "AI providers & MCP servers..."
section_gap
echo -e "     ${GRAY}FreeClaw needs at least one AI provider to answer. Add one${RESET}"
echo -e "     ${GRAY}from the web UI after install — any OpenAI-compatible endpoint:${RESET}"
section_gap
info "open the web UI, click ${BOLD}⚙ Settings${RESET} → ${BOLD}Providers${RESET},"
info "and paste in a URL, API key, and model. Free options that work:"
info "  ${LIME}aistudio.google.com${RESET} (Google AI)  ·  ${LIME}cloud.cerebras.ai${RESET} (Cerebras)"
section_gap
info "The same Settings page manages ${BOLD}MCP servers${RESET} (external tools —"
info "GitHub, search, databases) and your ${BOLD}.env${RESET} — no file editing needed."

section_gap
divider
section_gap

# ── Done ─────────────────────────────────────

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)

echo -e "   ${LIME}${BOLD}Installation complete!${RESET}"
echo ""
echo -e "   ${GRAY}FreeClaw is running in Docker and will come back up with Docker.${RESET}"
echo -e "   ${GRAY}Open the web UI in your browser:${RESET}"
echo ""
echo -e "   ${BG_DARK}   ${LIME}${BOLD}http://localhost:6767${RESET}${BG_DARK}   ${RESET}"
echo ""
if [[ -n "$LAN_IP" ]]; then
    echo -e "   ${DIM}${GRAY}From other devices on your network: http://${LAN_IP}:6767${RESET}"
    echo -e "   ${DIM}${GRAY}(set Settings → Custom Domain to that address so file links match)${RESET}"
    echo ""
fi
echo -e "   ${YELLOW}First step:${RESET} ${GRAY}open ${BOLD}⚙ Settings → Providers${RESET}${GRAY} and add an AI provider —${RESET}"
echo -e "   ${GRAY}FreeClaw can't answer until at least one is configured.${RESET}"
echo ""
echo -e "   ${DIM}${GRAY}The built-in OpenAI-compatible API is available at:${RESET}"
echo -e "   ${DIM}${GRAY}  http://localhost:6767/v1  (toggle on/off from the homepage)${RESET}"
echo -e "   ${DIM}${GRAY}  Use your FreeClaw password as the Bearer token.${RESET}"
echo ""
echo -e "   ${DIM}${GRAY}To chat from the terminal:  ${RESET}${LIME}${BOLD}freeclaw${RESET}"
echo -e "   ${DIM}${GRAY}To update later, run: ${RESET}${GRAY}./update-mac.sh${RESET}"
echo -e "   ${DIM}${GRAY}Logs: ${RESET}${GRAY}docker compose -f docker/docker-compose.yml logs -f${RESET}"
echo ""
divider
echo ""
