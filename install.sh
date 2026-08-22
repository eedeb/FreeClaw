#!/bin/bash
set -e

# ─────────────────────────────────────────────
#  FreeClaw — Installer (Linux / systemd)
#  github.com/eedeb/FreeClaw
#
#  macOS has no systemd; it runs FreeClaw in Docker instead. See
#  install-mac.sh.
# ─────────────────────────────────────────────

# Top-level paths that only make sense for the Docker/macOS install. Excluded
# from the checkout below so a Linux install doesn't carry container files it
# never uses. install-mac.sh does the mirror image of this.
MAC_ONLY=(
    "/docker/"
    "/install-mac.sh"
    "/update-mac.sh"
    "/uninstall-mac.sh"
    "/.dockerignore"
)

# Development-only paths, skipped on every platform: the benchmark harness is
# run against an install rather than by it, and the telemetry collector is
# deployed to Cloudflare, not executed here. Keep in sync with install-mac.sh.
DEV_ONLY=(
    "/bench/"
    "/telemetry/"
)

# Colors & styles
RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"

BLACK="\033[0;30m"
GREEN="\033[0;32m"
LIME="\033[38;5;154m"       # #c8f04a-ish (256-color lime)
WHITE="\033[0;97m"
GRAY="\033[0;90m"
RED="\033[0;31m"
YELLOW="\033[0;33m"
CYAN="\033[0;36m"

BG_LIME="\033[48;5;154m"
BG_DARK="\033[48;5;234m"

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
    echo -e "   ${DIM}${GRAY}github.com/eedeb/FreeClaw${RESET}"
    echo ""
    echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"
    echo ""
}

step() {
    local num="$1"
    local msg="$2"
    echo -e "   ${BG_DARK} ${LIME}${BOLD}${num}${RESET}${BG_DARK} ${RESET} ${BOLD}${WHITE}${msg}${RESET}"
}

info() {
    echo -e "     ${GRAY}→${RESET}  $1"
}

success() {
    echo -e "     ${LIME}✓${RESET}  $1"
}

warn() {
    echo -e "     ${YELLOW}!${RESET}  $1"
}

error() {
    echo -e "     ${RED}✗${RESET}  $1"
}

section_gap() {
    echo ""
}

divider() {
    echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"
}

# Xvfb — the virtual display FreeClaw's sign-in browser runs on
# (src/browser_takeover.py). Without it that browser falls back to headless,
# and headless is precisely what Google and Microsoft sign-in refuse, so a
# feature that exists to get past a login wall can't. Tens of MB, and the only
# chance to install it: FreeClaw itself runs as an ordinary user and can't
# apt-get anything.
#
# Never fatal. This is one optional feature, and no package manager problem
# should cost someone the whole install — so every path here ends in a warning
# and carries on, and the Settings page says the same thing later if the
# browser starts up without a display.
install_xvfb() {
    if command -v Xvfb &>/dev/null; then
        success "Xvfb already present (sign-in browser)"
        return 0
    fi
    info "Installing Xvfb (virtual display for the sign-in browser)..."
    # Every call is `|| true`: `set -e` is on, and a package manager failing
    # here must not take the install down with it. Whether it worked is decided
    # below by looking for the binary, which is the only thing that matters.
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq || true
        sudo apt-get install -y -qq --no-install-recommends xvfb || true
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y -q xorg-x11-server-Xvfb || true
    elif command -v yum &>/dev/null; then
        sudo yum install -y -q xorg-x11-server-Xvfb || true
    elif command -v zypper &>/dev/null; then
        # By the file it provides, not a package name: openSUSE moved Xvfb from
        # xorg-x11-server to xorg-x11-server-extra, and the capability resolves
        # on both.
        sudo zypper --non-interactive install /usr/bin/Xvfb || true
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm --needed xorg-server-xvfb || true
    elif command -v apk &>/dev/null; then
        sudo apk add --quiet xvfb || true
    else
        warn "No supported package manager found — skipping Xvfb."
        warn "Install it yourself if you want to sign the agent into Google/Microsoft."
        return 0
    fi
    if command -v Xvfb &>/dev/null; then
        success "Xvfb installed"
    else
        warn "Couldn't install Xvfb — everything else is fine, but the sign-in"
        warn "browser will run headless, which Google and Microsoft refuse."
    fi
    return 0
}

# ── Preflight ────────────────────────────────

print_banner

step "0" "Checking prerequisites..."
section_gap

if [[ "$(uname -s)" == "Darwin" ]]; then
    error "This installer registers a systemd service, which macOS doesn't have."
    info "Use the macOS installer instead — it runs FreeClaw in Docker:"
    info "  ${LIME}curl -fsSL https://freeclaw.eedeb.dev/install-mac.sh | bash${RESET}"
    exit 1
fi

for cmd in git python3 sudo; do
    if command -v "$cmd" &>/dev/null; then
        success "${cmd} found"
    else
        error "${cmd} is required but not found."
        exit 1
    fi
done

section_gap
divider
section_gap

# ── Clone ────────────────────────────────────

step "1" "Cloning repository..."
section_gap
info "Fetching from github.com/eedeb/FreeClaw"

git clone --no-checkout https://github.com/eedeb/FreeClaw 2>&1 | sed 's/^/       /'
cd FreeClaw || exit 1

# Piping through sed masks git's exit status (a pipeline reports the last
# command's), so check PIPESTATUS rather than relying on set -e here.
checkout_main() {
    git checkout main 2>&1 | sed 's/^/       /'
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
        error "Checkout failed — see the output above."
        exit 1
    fi
}

# Check out everything except the Docker/macOS files. Non-cone mode is what
# allows negated patterns; it needs git 2.25+, so fall back to deleting the
# files after a normal checkout on anything older.
if git sparse-checkout init --no-cone &>/dev/null; then
    {
        echo '/*'
        for path in "${MAC_ONLY[@]}" "${DEV_ONLY[@]}"; do echo "!${path}"; done
    } | git sparse-checkout set --stdin
    checkout_main
    success "Repository ready (macOS-only and dev files skipped)"
else
    warn "git is too old for sparse-checkout — pruning after checkout instead"
    checkout_main
    for path in "${MAC_ONLY[@]}" "${DEV_ONLY[@]}"; do rm -rf ".${path}"; done
    success "Repository ready (macOS-only and dev files removed)"
fi

section_gap
divider
section_gap

# ── Virtual environment ───────────────────────

step "2" "Setting up Python environment..."
section_gap
info "Creating virtual environment"
python3 -m venv venv
success "Virtual environment created"

# Debian/Raspberry Pi OS Bookworm bundles pip 23.0.1, and pip 23.0 checks that a
# wheel's metadata name matches the name that was requested - comparing the raw
# strings, so "typing_extensions" fails to match "typing-extensions" and "Jinja2"
# fails to match "jinja2". piwheels, which every Raspberry Pi has configured
# system-wide, serves those exact spellings. The result is not a clean error: pip
# discards every candidate, backtracks to whatever ancient version has no floor
# (torch 2.1.2 paired with a typing-extensions from 2021), and where no such
# version exists it falls back to a source build whose own build dependencies
# then fail the identical check. pip 23.1 normalized the comparison. Upgrading
# first is what makes everything below resolve correctly on a Pi.
info "Upgrading pip"
venv/bin/pip install --upgrade pip -q
success "pip upgraded"

section_gap
divider
section_gap

# ── Dependencies ─────────────────────────────

step "3" "Installing dependencies..."
section_gap

_pip_install() {
    local label="$1"
    shift
    info "Installing ${label}..."
    venv/bin/pip install "$@" -q
    success "${label} installed"
}

_pip_install "web & API libs" -r requirements.txt

# The intent classifier (models/run_model.py) tokenises with NLTK, which needs
# this word table at runtime. Fetched here so the first message doesn't stall on
# a download, and so an install that later runs offline still classifies.
#
# Never fatal. run_model.py asks for the same table on import, so a machine that
# can't reach the network now gets another chance later — and an optional
# download must not take the whole install down with it.
info "Downloading NLTK tokenizer data..."
if venv/bin/python -c "import nltk; nltk.download('punkt_tab', quiet=True)" 2>/dev/null; then
    success "NLTK tokenizer data installed"
else
    warn "Couldn't fetch NLTK data; Classy will retry on first use."
fi

# A system package rather than a wheel, and optional — see install_xvfb().
install_xvfb

section_gap
divider
section_gap

# ── Directories ──────────────────────────────

step "4" "Setting up project directories..."
section_gap
mkdir -p Flask/static
success "Directories ready"

section_gap
divider
section_gap

# ── Configuration ────────────────────────────

step "5" "Configuration..."
section_gap
echo -e "     ${GRAY}Just a login password now — you'll add your AI provider(s)${RESET}"
echo -e "     ${GRAY}from the web UI after install (no API keys needed here).${RESET}"
section_gap

while true; do
    read -s -p "$(echo -e "     ${LIME}?${RESET}  Set the Web UI password: ")" fc_password < /dev/tty
    echo ""
    read -s -p "$(echo -e "     ${LIME}?${RESET}  Confirm password: ")" fc_password_confirm < /dev/tty
    echo ""

    if [[ "$fc_password" != "$fc_password_confirm" ]]; then
        warn "Passwords do not match — please try again."
        section_gap
    else
        break
    fi
done

secret_key=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# ── Telemetry opt-in ─────────────────────────
# Default is no: anything other than an explicit "y" leaves it off, and the
# empty answer from just hitting Enter lands there too.
section_gap
echo -e "     ${GRAY}Optional: send one anonymous ping so I can count installs.${RESET}"
echo -e "     ${GRAY}It contains a random ID, the FreeClaw version, your OS, and${RESET}"
echo -e "     ${GRAY}the word \"native\". That's the whole payload — no chats, no${RESET}"
echo -e "     ${GRAY}prompts, no API keys, no provider names.${RESET}"
echo -e "     ${GRAY}Sent once, never repeated. Off by default, and you can flip${RESET}"
echo -e "     ${GRAY}it either way later in ${BOLD}Settings${RESET}${GRAY}.${RESET}"
section_gap

read -p "$(echo -e "     ${LIME}?${RESET}  Send the install ping? [y/${BOLD}N${RESET}]: ")" fc_telemetry_answer < /dev/tty
case "$fc_telemetry_answer" in
    [Yy]*) fc_telemetry=1 ;;
    *)     fc_telemetry=0 ;;
esac
# Write .env: just login password + session secret. Chat providers are
# added afterward from Settings → Providers in the web UI (persisted into
# PROVIDER_* lists) — no API keys collected here. The one exception: NVIDIA
# NIM, only used for the image-description tool, has no Settings UI of its
# own — add NVIDIA_KEY=... to .env by hand and restart if you want that
# tool to work.
printf 'FC_PASSWORD=%s\n' "$fc_password" > .env
chmod 600 .env
printf 'SECRET_KEY=%s\n' "$secret_key" >> .env
printf 'FC_TELEMETRY=%s\n' "$fc_telemetry" >> .env
success "Password saved"
success "Session secret generated"
if [[ "$fc_telemetry" == "1" ]]; then
    success "Anonymous install ping enabled — thank you"
else
    success "Telemetry off"
fi

section_gap
divider
section_gap

# ── systemd ──────────────────────────────────

step "6" "Registering systemd services and CLI..."
section_gap

INSTALL_DIR=$(pwd)
USER_NAME=$(whoami)

info "Writing FreeClaw.service"
sudo tee /etc/systemd/system/FreeClaw.service > /dev/null <<EOF
[Unit]
Description=FreeClaw Flask Application
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 -m Flask.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

info "Enabling and starting FreeClaw..."
sudo systemctl enable FreeClaw.service
sudo systemctl start FreeClaw.service
success "FreeClaw service running"

info "Installing freeclaw CLI to /usr/local/bin..."
sudo tee /usr/local/bin/freeclaw > /dev/null <<EOF
#!/bin/bash
cd $INSTALL_DIR
$INSTALL_DIR/venv/bin/python3 -m src.cli "\$@"
EOF
sudo chmod +x /usr/local/bin/freeclaw
success "CLI installed — run 'freeclaw' from anywhere"

section_gap
divider
section_gap

# ── Permissions ──────────────────────────────

chmod +x update.sh
chmod +x uninstall.sh

# ── MCP servers ──────────────────────────────

step "7" "AI providers & MCP servers..."
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

IP=$(hostname -I | awk '{print $1}')

echo -e "   ${LIME}${BOLD}Installation complete!${RESET}"
echo ""
echo -e "   ${GRAY}FreeClaw is running and will auto-start on boot.${RESET}"
echo -e "   ${GRAY}Open the web UI in your browser:${RESET}"
echo ""
echo -e "   ${BG_DARK}   ${LIME}${BOLD}http://${IP}:6767${RESET}${BG_DARK}   ${RESET}"
echo ""
echo -e "   ${YELLOW}First step:${RESET} ${GRAY}open ${BOLD}⚙ Settings → Providers${RESET}${GRAY} and add an AI provider —${RESET}"
echo -e "   ${GRAY}FreeClaw can't answer until at least one is configured.${RESET}"
echo ""
echo -e "   ${DIM}${GRAY}The built-in OpenAI-compatible API is available at:${RESET}"
echo -e "   ${DIM}${GRAY}  http://${IP}:6767/v1  (toggle on/off from the homepage)${RESET}"
echo -e "   ${DIM}${GRAY}  Use your FreeClaw password as the Bearer token.${RESET}"
echo ""
echo -e "   ${DIM}${GRAY}To chat from the terminal:  ${RESET}${LIME}${BOLD}freeclaw${RESET}"
echo -e "   ${DIM}${GRAY}To update later, run: ${RESET}${GRAY}./update.sh${RESET}"
echo -e "   ${DIM}${GRAY}Logs: ${RESET}${GRAY}journalctl -u FreeClaw -f${RESET}"
echo ""
divider
echo ""