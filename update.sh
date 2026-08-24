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

# torch, classy-ai and sentence-transformers backed the old Classy classifier.
# What replaced it (models/run_model.py) is plain Python over JSON weights, so
# all three are dead weight now — several hundred MB of it, on machines that are
# often running from an SD card.
#
# This is the only place that cleanup can happen. Syncing requirements.txt only
# ever adds, and pip never removes a package just because nothing requires it
# any more, so an existing install has no other way to shed them.
#
# Never fatal, for the same reason as ensure_xvfb: an update that dies while
# tidying up is worse than one that leaves a few old wheels behind.
remove_legacy_ml() {
    local candidates=(torch classy-ai sentence-transformers)
    local found=()
    local p

    for p in "${candidates[@]}"; do
        if venv/bin/pip show "$p" &>/dev/null; then
            found+=("$p")
        fi
    done

    if [[ ${#found[@]} -eq 0 ]]; then
        return 0
    fi

    info "Removing packages the new classifier doesn't need (${found[*]})..."
    venv/bin/pip uninstall -y -q "${found[@]}" &>/dev/null || true

    # torch is the only one of the three that drags a CUDA stack in behind it
    # (nvidia-cublas, cudnn, nccl, triton — up to ~3GB where the CPU index
    # wasn't used). Those are orphaned the moment it goes, and nothing else
    # FreeClaw installs touches CUDA.
    #
    # Deliberately not a general orphan sweep: torch also shares jinja2,
    # filelock and fsspec with packages FreeClaw does need, and flask would go
    # down with jinja2. Matched by exact name against pip's own list so this
    # can't reach something that merely looks similar.
    if printf '%s\n' "${found[@]}" | grep -qx torch; then
        local orphans
        orphans=$(venv/bin/pip list --format=freeze 2>/dev/null \
            | cut -d= -f1 \
            | grep -E '^(nvidia-[a-z0-9_-]+|triton)$' || true)
        if [[ -n "$orphans" ]]; then
            info "Removing the CUDA libraries torch pulled in..."
            venv/bin/pip uninstall -y -q $orphans &>/dev/null || true
        fi
    fi

    # Report what actually went, not what was attempted — a uninstall that
    # failed on a permissions problem would otherwise be invisible.
    local still=()
    for p in "${found[@]}"; do
        if venv/bin/pip show "$p" &>/dev/null; then
            still+=("$p")
        fi
    done

    if [[ ${#still[@]} -eq 0 ]]; then
        success "Removed ${found[*]}"
    else
        warn "Couldn't remove: ${still[*]} — FreeClaw still works, just larger."
    fi
    return 0
}

# The new classifier tokenises with NLTK, which needs a word table that pip
# doesn't carry. Installs predating it have never fetched one, and without it
# models/run_model.py raises on the first classify(). install.sh does this for
# fresh installs; this is the same step for everyone already running.
ensure_nltk_data() {
    if venv/bin/python -c "import nltk; nltk.data.find('tokenizers/punkt_tab')" &>/dev/null; then
        return 0
    fi
    info "Downloading NLTK tokenizer data..."
    if venv/bin/python -c "import nltk; nltk.download('punkt_tab', quiet=True)" &>/dev/null; then
        success "NLTK tokenizer data installed"
    else
        warn "Couldn't fetch NLTK data; Classy will retry on first use."
    fi
    return 0
}

# ── Options ──────────────────────────────────

# --no-service: do the update but never touch FreeClaw.service.
#
# For the "Update FreeClaw" button in Settings, which runs this script from
# inside the running server. Two reasons that path cannot use the normal one:
#
#   * No sudo. The service runs as the installing user (User= in the unit
#     install.sh writes), and `sudo systemctl` from a process with no tty
#     fails outright rather than prompting — with `set -e`, that would abort
#     the update at its first step.
#   * Stopping the service would kill this script. systemd's default
#     KillMode=control-group takes down everything in the unit's cgroup, and a
#     script spawned by the server is in it — so `systemctl stop` would end the
#     update mid-pull.
#
# So the server stays up for the whole update and restarts itself afterwards
# through /api/restart, which exits with code 42 and lets systemd respawn it.
# Nothing here needs the service to be down: git checkout and pip write files
# that the running process has already imported, and nothing reads them again
# until the restart.
NO_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --no-service) NO_SERVICE=1 ;;
        -h|--help)
            echo "Usage: ./update.sh [--no-service]"
            echo
            echo "  --no-service  Update without stopping or starting"
            echo "                FreeClaw.service. The caller is responsible for"
            echo "                restarting FreeClaw afterwards. Used by the"
            echo "                Update button in Settings."
            exit 0
            ;;
        *)
            error "Unknown option: $arg"
            echo "Try: ./update.sh --help"
            exit 1
            ;;
    esac
done

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

if [[ $NO_SERVICE -eq 1 ]]; then
    info "Leaving FreeClaw running (--no-service); it restarts itself at the end"
else
    info "Stopping FreeClaw service..."
    sudo systemctl stop FreeClaw.service
    success "Service stopped"
fi

section_gap
info "Pulling updates from origin/main..."
git checkout origin/main -- src/
git checkout origin/main -- Flask/templates/
git checkout origin/main -- Flask/main.py
git checkout origin/main -- requirements.txt 2>/dev/null || true
# src/telemetry.py reports this, and the ff-only merge below is skipped when
# the index is dirty — so refresh it explicitly rather than relying on that.
git checkout origin/main -- VERSION 2>/dev/null || true
# Same reasoning, and the reason Classy went missing on installs updating across
# the switch to it: models/ holds run_model.py and the JSON weights that
# src/agent.py imports at startup. Leaving it to the merge means that when the
# merge is blocked, `git reset --soft` moves HEAD without touching the working
# tree — so the files never land, and the next run reads LOCAL == REMOTE and
# reports "Already up to date" forever while the service crash-loops on the
# missing import.
#
# Overwrites local edits under models/, deliberately: everything there is
# shipped (weights, intents, run_model.py, train.py), same contract as src/.
git checkout origin/main -- models/ 2>/dev/null || true

# Advance local HEAD to match origin/main so git log is correct next run
git merge --ff-only origin/main 2>/dev/null || git reset --soft origin/main 2>/dev/null || true

success "Source files updated"

info "Restoring Flask/static/ (user files preserved)..."
mkdir -p Flask/static
# The Setup Wizard is deliberately NOT checked out here, in either direction.
#
# It only exists to walk a brand-new install through first-time setup, and
# anything running update.sh is by definition already set up — so an update has
# no one to onboard. It used to hand the wizard to installs that predated it,
# guarded by a marker file so a delete would stick; that is gone because the
# case it served (installs older than the wizard) has long since passed, and the
# marker only ever existed to stop the update resurrecting something the user
# had thrown away.
#
# Checking it out over an existing copy would be worse still: it is a live user
# folder, so that would wipe the conversation and context.md of anyone actually
# mid-setup. Installs that have the wizard keep exactly what they have,
# including any edits; installs without it stay without it.
#
# Flask/.wizard_installed is left alone rather than cleaned up — it is
# gitignored, harmless, and removing it would mean a version that still checks
# for it could re-add the wizard on a downgrade.
success "Static directory intact"

section_gap
info "Syncing dependencies..."
venv/bin/pip install -q -r requirements.txt 2>/dev/null || true
success "Dependencies up to date"

ensure_nltk_data
remove_legacy_ml

# System package, not a wheel, and only installed once — see ensure_xvfb().
ensure_xvfb

section_gap
divider
section_gap

# ── Restart ──────────────────────────────────

if [[ $NO_SERVICE -eq 1 ]]; then
    info "Not restarting the service (--no-service) — the caller does that"
else
    info "Restarting FreeClaw..."
    sudo systemctl start FreeClaw.service
    success "FreeClaw is running"
fi

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