#!/bin/bash
set -e

# ─────────────────────────────────────────────
#  FreeClaw — Installer
#  github.com/eedeb/FreeClaw
# ─────────────────────────────────────────────

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

# ── Preflight ────────────────────────────────

print_banner

step "0" "Checking prerequisites..."
section_gap

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
git clone https://github.com/eedeb/FreeClaw 2>&1 | sed 's/^/       /'
cd FreeClaw || exit 1
success "Repository ready"

section_gap
divider
section_gap

# ── Virtual environment ───────────────────────

step "2" "Setting up Python environment..."
section_gap
info "Creating virtual environment"
python3 -m venv venv
success "Virtual environment created"

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

_pip_install "PyTorch (CPU)" torch --index-url https://download.pytorch.org/whl/cpu
_pip_install "classy-ai" classy-ai
_pip_install "sentence-transformers" sentence_transformers --no-deps
_pip_install "web & API libs" flask openai ddgs beautifulsoup4 json-repair python-dotenv uvicorn
_pip_install "FastAPI" fastapi

section_gap
divider
section_gap

# ── Directories ──────────────────────────────

step "4" "Setting up project directories..."
section_gap
mkdir -p Flask/static
mkdir -p Flask/templates/agent
touch Flask/static/context.md
success "Directories ready"

section_gap
divider
section_gap

# ── API Key ──────────────────────────────────

step "5" "Configuration..."
section_gap
echo -e "     ${GRAY}You'll need a free Groq API key from${RESET} ${LIME}console.groq.com${RESET}"
section_gap

read -p "$(echo -e "     ${LIME}?${RESET}  Groq API key: ")" api_key < /dev/tty
printf 'API_KEY=%s\n' "$api_key" > .env
chmod 600 .env
success "API key saved to .env"

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
printf 'FC_PASSWORD=%s\n' "$fc_password" >> .env
printf 'SECRET_KEY=%s\n' "$secret_key" >> .env
success "Password saved"
success "Session secret generated"

section_gap
divider
section_gap

# ── systemd ──────────────────────────────────

step "6" "Registering systemd services..."
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

info "Writing FreeClawAPI.service"
sudo tee /etc/systemd/system/FreeClawAPI.service > /dev/null <<EOF
[Unit]
Description=FreeClaw API service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/uvicorn src.api:app --host 0.0.0.0 --port 8080
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

info "Enabling FreeClawAPI..."
sudo systemctl enable FreeClawAPI.service
success "FreeClawAPI service enabled"

section_gap
divider
section_gap

# ── Home Assistant ───────────────────────────

chmod +x ha_setup.sh
chmod +x update.sh

step "7" "Optional: Home Assistant integration..."
section_gap
echo -e "     ${GRAY}Connect FreeClaw to your smart home — control devices and${RESET}"
echo -e "     ${GRAY}announce responses via Alexa using Home Assistant.${RESET}"
section_gap

if [[ -x ha_setup.sh ]]; then
    read -p "$(echo -e "     ${LIME}?${RESET}  Set up Home Assistant? ${GRAY}(y/n)${RESET} ")" answer < /dev/tty
    if [[ "$answer" == "y" ]]; then
        section_gap
        ./ha_setup.sh
    else
        info "Skipping Home Assistant setup — run ${BOLD}./ha_setup.sh${RESET} anytime to add it later."
    fi
else
    warn "ha_setup.sh not found or not executable, skipping."
fi

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
echo -e "   ${DIM}${GRAY}To update later, run: ${RESET}${GRAY}./update.sh${RESET}"
echo -e "   ${DIM}${GRAY}Logs: ${RESET}${GRAY}journalctl -u FreeClaw -f${RESET}"
echo ""
divider
echo ""