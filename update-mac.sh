#!/bin/bash
# See install-mac.sh — pipefail keeps a failed pip install from being masked
# by the indent filter it's piped through.
set -eo pipefail

# ─────────────────────────────────────────────
#  FreeClaw — Updater (macOS)
#  github.com/eedeb/FreeClaw
#
#  Run it from your install directory (~/.freeclaw by default):
#
#      ~/.freeclaw/update-mac.sh
#
#  Or press "Update FreeClaw" in Settings, which runs this same script with
#  --no-restart from inside the running server.
#
#  The counterpart of update.sh on Linux, and the two do the same work: pull
#  the tracked source paths, sync dependencies, leave every byte of user data
#  alone. What differs is only what supervises the server — systemd there, the
#  menu bar app (mac/tray.py) here.
# ─────────────────────────────────────────────

RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"

LIME="\033[38;5;154m"
WHITE="\033[0;97m"
GRAY="\033[0;90m"
RED="\033[0;31m"
YELLOW="\033[0;33m"

info()    { echo -e "     ${GRAY}→${RESET}  $1"; }
success() { echo -e "     ${LIME}✓${RESET}  $1"; }
warn()    { echo -e "     ${YELLOW}!${RESET}  $1"; }
error()   { echo -e "     ${RED}✗${RESET}  $1"; }
divider() { echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"; }
section_gap() { echo ""; }
indent()  { sed 's/^/       /'; }

# ── Options ──────────────────────────────────

# --no-restart: do the update but never touch the running server.
#
# For the "Update FreeClaw" button in Settings, which runs this script from
# inside that very server. Restarting from here would kill the update
# half-finished: the process this script would be signalling is its own
# grandparent's child, and this script is inside the tree that goes with it.
#
# So the server stays up for the whole update and restarts itself afterwards
# through /api/restart, which exits with code 42 and lets the menu bar app
# respawn it. Nothing here needs it to be down: git checkout and pip write
# files the running process has already imported, and nothing reads them
# again until the restart.
NO_RESTART=0
for arg in "$@"; do
    case "$arg" in
        --no-restart) NO_RESTART=1 ;;
        -h|--help)
            echo "Usage: ./update-mac.sh [--no-restart]"
            echo
            echo "  --no-restart  Update without restarting the server. The"
            echo "                caller is responsible for restarting FreeClaw"
            echo "                afterwards. Used by the Update button in"
            echo "                Settings."
            exit 0
            ;;
        *)
            error "Unknown option: $arg"
            echo "Try: ./update-mac.sh --help"
            exit 1
            ;;
    esac
done

# ── Header ───────────────────────────────────

echo ""
echo -e "   ${LIME}${BOLD}FreeClaw${RESET} ${GRAY}·${RESET} ${BOLD}${WHITE}Updater${RESET} ${GRAY}(macOS)${RESET}"
echo ""
divider
section_gap

# ── Preflight ────────────────────────────────

# The install directory, not the caller's. The Update button runs this with
# the server's working directory, and a user running it by hand may well be
# somewhere else entirely.
INSTALL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/src" || ! -d "$INSTALL_DIR/.git" ]]; then
    error "This doesn't look like a FreeClaw install directory."
    info "Run the copy inside your install: ${LIME}~/.freeclaw/update-mac.sh${RESET}"
    section_gap
    exit 1
fi

PY="$INSTALL_DIR/python/bin/python3"
if [[ ! -x "$PY" ]]; then
    error "The private Python is missing from ${INSTALL_DIR}/python."
    info "Re-run the installer, which puts it back and keeps your data:"
    info "  ${LIME}curl -fsSL https://freeclaw.eedeb.dev/install-mac.sh | bash${RESET}"
    section_gap
    exit 1
fi

# ── Check for updates ────────────────────────

info "Fetching latest changes from GitHub..."
git fetch --depth 1 origin main 2>&1 | indent

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

# Path by path, never a bare checkout. Flask/static is where every chat,
# upload and context.md lives, and the Setup Wizard inside it is a *tracked*
# folder that becomes a live user the moment somebody talks to it — so
# `git checkout .` would reset their conversation. Same list as the upgrade
# path in install-mac.sh, and the same reasoning as update.sh on Linux.
#
# models/ is named explicitly rather than left to the merge: it holds
# run_model.py and the JSON weights src/agent.py imports at startup, and when
# the merge is blocked `git reset --soft` moves HEAD without touching the
# working tree — so the files would never land, and the next run would read
# LOCAL == REMOTE and report "already up to date" forever while the server
# crash-looped on the missing import.
for path in src models mac Flask/main.py Flask/templates requirements.txt \
            VERSION update-mac.sh uninstall-mac.sh; do
    git checkout origin/main -- "$path" 2>/dev/null || true
done

# Advance local HEAD to match origin/main so `git log` is correct next run.
# Sparse-checkout patterns are preserved, so the other platforms' files stay
# out of the tree.
git merge --ff-only origin/main 2>/dev/null || git reset --soft origin/main 2>/dev/null || true

success "Source files updated"
info "Flask/static/ left untouched (chats, uploads and context.md preserved)"

section_gap
info "Syncing dependencies..."
"$PY" -m pip install -q --disable-pip-version-check --no-cache-dir \
    -r requirements.txt -r mac/requirements-mac.txt 2>&1 | indent || \
    warn "Some dependencies didn't install — see above; the update continues"
success "Dependencies up to date"

# The classifier tokenises with NLTK, which needs a word table pip doesn't
# carry. Never fatal: models/run_model.py falls back to fetching it on first
# use.
if ! "$PY" -c "import nltk; nltk.data.find('tokenizers/punkt_tab')" &>/dev/null; then
    info "Fetching the classifier's tokenizer data..."
    "$PY" -c "import nltk; nltk.download('punkt_tab', quiet=True)" &>/dev/null \
        || warn "Couldn't fetch it; Classy will retry on first use."
fi

chmod +x update-mac.sh uninstall-mac.sh 2>/dev/null || true

section_gap
divider
section_gap

# ── Restart ──────────────────────────────────

if [[ $NO_RESTART -eq 1 ]]; then
    info "Not restarting (--no-restart) — the caller does that"
else
    server_pid=""
    [[ -f freeclaw-server.pid ]] && \
        server_pid=$(head -n1 freeclaw-server.pid 2>/dev/null | tr -dc '0-9')

    if [[ -n "$server_pid" ]] && ps -p "$server_pid" -o command= 2>/dev/null | grep -q "Flask.main"; then
        # Stop the server and let the menu bar app put it back — that is what
        # it is for, and it is the only process that knows how to start one.
        info "Restarting the server..."
        kill "$server_pid" 2>/dev/null || true
        for _ in $(seq 1 40); do
            curl -fsS -o /dev/null "http://127.0.0.1:6767/login" 2>/dev/null && break
            sleep 1
        done
        if curl -fsS -o /dev/null "http://127.0.0.1:6767/login" 2>/dev/null; then
            success "FreeClaw is running the new version"
        else
            warn "It hasn't answered yet — check ${INSTALL_DIR}/logs/tray.log"
        fi
    else
        info "FreeClaw isn't running — open it from ~/Applications or Spotlight"
    fi
fi

# The menu bar app is running the code it was started with, so a change to
# mac/tray.py itself only takes effect when the app is next started. Said out
# loud rather than acted on: quitting it from here would take the server — and
# this script, on the Settings path — down with it.
if ! git diff --quiet "$LOCAL" "$REMOTE" -- mac/tray.py 2>/dev/null; then
    section_gap
    info "The menu bar app changed in this update. Quit FreeClaw from the menu"
    info "bar and open it again to pick that up — the server is already current."
fi

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
