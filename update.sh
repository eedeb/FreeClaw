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

# Advance local HEAD to match origin/main so git log is correct next run
git merge --ff-only origin/main 2>/dev/null || git reset --soft origin/main 2>/dev/null || true

success "Source files updated"

info "Restoring Flask/static/ (user files preserved)..."
mkdir -p Flask/static
success "Static directory intact"

section_gap
info "Syncing dependencies..."
venv/bin/pip install -q -r requirements.txt 2>/dev/null || true
success "Dependencies up to date"

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