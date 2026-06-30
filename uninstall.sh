#!/bin/bash
set -e

# ─────────────────────────────────────────────
#  FreeClaw — Uninstaller
#  github.com/eedeb/FreeClaw
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

echo ""
echo -e "   ${RED}${BOLD}FreeClaw Uninstaller${RESET}"
echo ""
divider
echo ""

# ── Root check ───────────────────────────────

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

# ── Confirm ──────────────────────────────────

warn "This will remove the FreeClaw service, CLI, and all installation files."
echo ""
read -p "$(echo -e "     ${LIME}?${RESET}  Are you sure you want to uninstall FreeClaw? ${GRAY}(yes/no)${RESET} ")" confirm < /dev/tty
echo ""

if [[ "$confirm" != "yes" ]]; then
    info "Uninstall cancelled."
    exit 0
fi

divider
echo ""

# ── Resolve install directory ─────────────────

SERVICE_FILE=/etc/systemd/system/FreeClaw.service
INSTALL_DIR=""

if [[ -f "$SERVICE_FILE" ]]; then
    INSTALL_DIR=$(grep -Po '(?<=WorkingDirectory=).*' "$SERVICE_FILE" || true)
fi

# ── Stop & disable service ────────────────────

step "1" "Stopping FreeClaw service..."
echo ""

if systemctl is-active --quiet FreeClaw.service 2>/dev/null; then
    systemctl stop FreeClaw.service
    success "Service stopped"
else
    info "Service was not running"
fi

if systemctl is-enabled --quiet FreeClaw.service 2>/dev/null; then
    systemctl disable FreeClaw.service
    success "Service disabled"
else
    info "Service was not enabled"
fi

echo ""
divider
echo ""

# ── Remove service file ───────────────────────

step "2" "Removing systemd unit..."
echo ""

if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    success "Removed $SERVICE_FILE"
else
    warn "$SERVICE_FILE not found — skipping"
fi

echo ""
divider
echo ""

# ── Remove CLI ────────────────────────────────

step "3" "Removing CLI..."
echo ""

CLI=/usr/local/bin/freeclaw
if [[ -f "$CLI" ]]; then
    rm -f "$CLI"
    success "Removed $CLI"
else
    warn "$CLI not found — skipping"
fi

echo ""
divider
echo ""

# ── Remove install directory ──────────────────

step "4" "Removing installation files..."
echo ""

if [[ -n "$INSTALL_DIR" && -d "$INSTALL_DIR" ]]; then
    info "Removing $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    success "Installation directory removed"
elif [[ -z "$INSTALL_DIR" ]]; then
    warn "Could not determine install directory from service file — skipping."
    warn "Remove the FreeClaw folder manually if needed."
else
    warn "Directory '$INSTALL_DIR' not found — skipping"
fi

echo ""
divider
echo ""

echo -e "   ${LIME}${BOLD}FreeClaw has been uninstalled.${RESET}"
echo ""
echo -e "   ${GRAY}Thanks for using FreeClaw — hope to see you again.${RESET}"
echo ""
