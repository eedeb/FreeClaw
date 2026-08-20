#!/bin/bash
set -e

# ─────────────────────────────────────────────
#  FreeClaw — Updater
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

# ── Helpers ──────────────────────────────────

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

divider() {
    echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"
}

section_gap() {
    echo ""
}

# Xvfb — the virtual display FreeClaw's sign-in browser runs on
# (src/browser_takeover.py). Here as well as in install.sh because an install
# made before this existed has no other way to get it: FreeClaw runs as an
# ordinary user and can't apt-get anything itself, so without this the sign-in
# browser stays headless forever, and headless is exactly what Google and
# Microsoft sign-in refuse.
#
# A no-op on every run after the first, and never fatal — an update must not
# fail over an optional feature.
ensure_xvfb() {
    command -v Xvfb &>/dev/null && return 0
    info "Installing Xvfb (virtual display for the sign-in browser)..."
    # Every call is `|| true`: `set -e` is on, and a package manager problem
    # must not abort the update. The check afterwards is what decides.
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
        warn "No supported package manager — skipping Xvfb (sign-in browser stays headless)."
        return 0
    fi
    if command -v Xvfb &>/dev/null; then
        success "Xvfb installed — the sign-in browser can now run headful"
    else
        warn "Couldn't install Xvfb; the sign-in browser will run headless."
    fi
    return 0
}

# ── Header ───────────────────────────────────

echo ""
echo -e "   ${LIME}${BOLD}FreeClaw${RESET} ${GRAY}·${RESET} ${BOLD}${WHITE}Updater${RESET}"
echo ""
divider
section_gap

# ── Preflight ────────────────────────────────

INSTALL_DIR=$(pwd)

if [[ ! -f "$INSTALL_DIR/.env" || ! -d "$INSTALL_DIR/src" ]]; then
    error "Run this script from your FreeClaw installation directory."
    section_gap
    exit 1
fi

# ── Check for updates ────────────────────────

info "Fetching latest changes from GitHub..."
git fetch origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
LOCAL_SHORT="${LOCAL:0:7}"
REMOTE_SHORT="${REMOTE:0:7}"

section_gap

if [[ "$LOCAL" == "$REMOTE" ]]; then
    success "Already up to date ${GRAY}(${LOCAL_SHORT})${RESET}"
    section_gap
    divider
    echo ""
    exit 0
fi

echo -e "     ${GRAY}Current:${RESET}  ${YELLOW}${LOCAL_SHORT}${RESET}"
echo -e "     ${GRAY}Latest: ${RESET}  ${LIME}${REMOTE_SHORT}${RESET}"
section_gap
divider
section_gap

# ── Apply update ─────────────────────────────

info "Stopping FreeClaw service..."
sudo systemctl stop FreeClaw.service
success "Service stopped"

section_gap
info "Pulling updates from origin/main..."
git checkout origin/main -- src/
git checkout origin/main -- Flask/templates/
git checkout origin/main -- Flask/main.py
git checkout origin/main -- requirements.txt 2>/dev/null || true
# src/telemetry.py reports this, and the ff-only merge below is skipped when
# the index is dirty — so refresh it explicitly rather than relying on that.
git checkout origin/main -- VERSION 2>/dev/null || true

# Advance local HEAD to match origin/main so git log is correct next run
git merge --ff-only origin/main 2>/dev/null || git reset --soft origin/main 2>/dev/null || true

success "Source files updated"

info "Restoring Flask/static/ (user files preserved)..."
mkdir -p Flask/static
# The Setup Wizard is the one tracked thing under static/, and it's a live user
# folder — so never check it out over an existing copy, which would wipe the
# conversation and context.md of anyone using it. It's also deletable from the
# home page, and a delete has to stick: the marker records that this install
# has already been given the wizard once, so an update never resurrects it.
# Installs that predate the wizard have no marker and no folder, and get it on
# their next update; everyone else is left exactly as they are.
WIZARD_DIR="Flask/static/Setup Wizard"
WIZARD_MARKER="Flask/.wizard_installed"
if [[ -d "$WIZARD_DIR" ]]; then
    touch "$WIZARD_MARKER"
elif [[ ! -f "$WIZARD_MARKER" ]]; then
    if git checkout origin/main -- "$WIZARD_DIR/" 2>/dev/null; then
        touch "$WIZARD_MARKER"
        info "Added the Setup Wizard user"
    fi
fi
success "Static directory intact"

section_gap
info "Syncing dependencies..."
venv/bin/pip install -q -r requirements.txt 2>/dev/null || true
success "Dependencies up to date"

# System package, not a wheel, and only installed once — see ensure_xvfb().
ensure_xvfb

section_gap
divider
section_gap

# ── Restart ──────────────────────────────────

info "Restarting FreeClaw..."
sudo systemctl start FreeClaw.service
success "FreeClaw is running"

section_gap
divider
section_gap

# ── Summary ──────────────────────────────────

echo -e "   ${LIME}${BOLD}Update complete!${RESET}"
section_gap
echo -e "   ${GRAY}Latest commits:${RESET}"

# Read from origin/main so commits are always fresh from GitHub
git log origin/main --oneline -5 | while IFS= read -r line; do
    hash="${line:0:7}"
    msg="${line:8}"
    echo -e "     ${LIME}${hash}${RESET}  ${GRAY}${msg}${RESET}"
done

section_gap
divider
echo ""