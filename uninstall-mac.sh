#!/bin/bash
set -eo pipefail

# ─────────────────────────────────────────────
#  FreeClaw — Uninstaller (macOS / Docker)
#  github.com/eedeb/FreeClaw
#
#  Run this from your FreeClaw installation directory. No sudo needed unless
#  the CLI wrapper landed in /usr/local/bin.
# ─────────────────────────────────────────────

RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"
LIME="\033[38;5;154m"
WHITE="\033[0;97m"
GRAY="\033[0;90m"
RED="\033[0;31m"
YELLOW="\033[0;33m"
BG_DARK="\033[48;5;234m"

info()    { echo -e "     ${GRAY}→${RESET}  $1"; }
success() { echo -e "     ${LIME}✓${RESET}  $1"; }
warn()    { echo -e "     ${YELLOW}!${RESET}  $1"; }
error()   { echo -e "     ${RED}✗${RESET}  $1"; }
step()    { echo -e "   ${BG_DARK} ${LIME}${BOLD}${1}${RESET}${BG_DARK} ${RESET} ${BOLD}${WHITE}${2}${RESET}"; }
divider() { echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"; }
indent()  { sed 's/^/       /'; }

COMPOSE="docker compose -f docker/docker-compose.yml"

echo ""
echo -e "   ${RED}${BOLD}FreeClaw Uninstaller${RESET} ${GRAY}(macOS / Docker)${RESET}"
echo ""
divider
echo ""

# ── Preflight ────────────────────────────────

INSTALL_DIR=$(pwd)

if [[ ! -f "$INSTALL_DIR/docker/docker-compose.yml" || ! -d "$INSTALL_DIR/src" ]]; then
    error "Run this script from your FreeClaw installation directory."
    echo ""
    exit 1
fi

# ── Confirm ──────────────────────────────────

warn "This will remove the FreeClaw container, image, CLI, and all"
warn "installation files — including your chats, uploads and context.md."
echo ""
read -p "$(echo -e "     ${LIME}?${RESET}  Are you sure you want to uninstall FreeClaw? ${GRAY}(yes/no)${RESET} ")" confirm < /dev/tty
echo ""

if [[ "$confirm" != "yes" ]]; then
    info "Uninstall cancelled."
    exit 0
fi

divider
echo ""

# ── Stop & remove container ───────────────────

step "1" "Stopping and removing the container..."
echo ""

if docker info &>/dev/null; then
    $COMPOSE down 2>&1 | indent || true
    success "Container stopped and removed"
else
    warn "Docker isn't running — skipping container removal"
    warn "Start Docker Desktop and run '$COMPOSE down' to finish this step"
fi

echo ""
divider
echo ""

# ── Remove image ──────────────────────────────

step "2" "Removing the image..."
echo ""

if docker info &>/dev/null && [[ -n "$(docker images -q freeclaw:local 2>/dev/null)" ]]; then
    docker rmi freeclaw:local 2>&1 | indent || true
    success "Image freeclaw:local removed"
else
    warn "Image freeclaw:local not found — skipping"
fi

echo ""
divider
echo ""

# ── Remove CLI ────────────────────────────────

step "3" "Removing CLI..."
echo ""

removed_cli=""
for CLI in /usr/local/bin/freeclaw "$HOME/.local/bin/freeclaw"; do
    if [[ -f "$CLI" ]]; then
        if rm -f "$CLI" 2>/dev/null; then
            success "Removed $CLI"
            removed_cli="yes"
        elif sudo rm -f "$CLI" 2>/dev/null; then
            success "Removed $CLI"
            removed_cli="yes"
        else
            warn "Could not remove $CLI — remove it manually"
        fi
    fi
done
[[ -z "$removed_cli" ]] && warn "No freeclaw CLI found — skipping"

echo ""
divider
echo ""

# ── Remove install directory ──────────────────

step "4" "Removing installation files..."
echo ""

info "Removing $INSTALL_DIR"
# Step outside the directory first so removing it doesn't pull the rug out
# from under the running shell.
cd "$(dirname "$INSTALL_DIR")" || exit 1
rm -rf "$INSTALL_DIR"
success "Installation directory removed"

echo ""
divider
echo ""

echo -e "   ${LIME}${BOLD}FreeClaw has been uninstalled.${RESET}"
echo ""
echo -e "   ${GRAY}Thanks for using FreeClaw — hope to see you again.${RESET}"
echo ""
