#!/bin/bash
# See install-mac.sh — pipefail keeps a failed rebuild from being masked by
# the indent filter it's piped through.
set -eo pipefail

# ─────────────────────────────────────────────
#  FreeClaw — Updater (macOS / Docker)
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

info()    { echo -e "     ${GRAY}→${RESET}  $1"; }
success() { echo -e "     ${LIME}✓${RESET}  $1"; }
warn()    { echo -e "     ${YELLOW}!${RESET}  $1"; }
error()   { echo -e "     ${RED}✗${RESET}  $1"; }
divider() { echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"; }
section_gap() { echo ""; }
indent()  { sed 's/^/       /'; }

COMPOSE="docker compose -f docker/docker-compose.yml"

# ── Header ───────────────────────────────────

echo ""
echo -e "   ${LIME}${BOLD}FreeClaw${RESET} ${GRAY}·${RESET} ${BOLD}${WHITE}Updater${RESET} ${GRAY}(macOS / Docker)${RESET}"
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

if [[ ! -f "$INSTALL_DIR/docker/docker-compose.yml" ]]; then
    error "docker/docker-compose.yml not found — this looks like a native install."
    info "Use ./update.sh instead."
    section_gap
    exit 1
fi

if ! docker info &>/dev/null; then
    error "Docker isn't running. Start Docker Desktop and try again."
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

info "Pulling updates from origin/main..."
git checkout origin/main -- src/
git checkout origin/main -- Flask/templates/
git checkout origin/main -- Flask/main.py
git checkout origin/main -- requirements.txt 2>/dev/null || true
# src/telemetry.py reports this, and the ff-only merge below is skipped when
# the index is dirty — so refresh it explicitly rather than relying on that.
git checkout origin/main -- VERSION 2>/dev/null || true
git checkout origin/main -- docker/ 2>/dev/null || true

# Advance local HEAD to match origin/main so git log is correct next run.
# Sparse-checkout patterns are preserved, so the Linux-only scripts stay out.
git merge --ff-only origin/main 2>/dev/null || git reset --soft origin/main 2>/dev/null || true

success "Source files updated"

info "Flask/static/ left untouched (user files preserved)"
mkdir -p Flask/static logs

section_gap
divider
section_gap

# ── Rebuild & restart ────────────────────────

# The image COPYs the source in, so new code only takes effect after a
# rebuild. Dependency changes in requirements.txt are picked up here too.
info "Rebuilding the container image..."
$COMPOSE build 2>&1 | indent
success "Image rebuilt"

section_gap
info "Restarting FreeClaw..."
$COMPOSE up -d 2>&1 | indent
success "FreeClaw is running"

section_gap
divider
section_gap

# ── Summary ──────────────────────────────────

echo -e "   ${LIME}${BOLD}Update complete!${RESET}"
section_gap
echo -e "   ${GRAY}Latest commits:${RESET}"

git log origin/main --oneline -5 | while IFS= read -r line; do
    hash="${line:0:7}"
    msg="${line:8}"
    echo -e "     ${LIME}${hash}${RESET}  ${GRAY}${msg}${RESET}"
done

section_gap
divider
echo ""
