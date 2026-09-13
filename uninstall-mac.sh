#!/bin/bash
set -eo pipefail

# ─────────────────────────────────────────────
#  FreeClaw — Uninstaller (macOS)
#  github.com/eedeb/FreeClaw
#
#  A copy of this script is installed alongside FreeClaw, so the usual way to
#  run it is from the install itself — it defaults to removing the install it
#  is sitting in:
#
#      ~/.freeclaw/uninstall-mac.sh
#
#  Stops FreeClaw, removes the app, the login item and the `freeclaw` command,
#  and deletes the program files.
#
#  Your data stays by default. Chats, uploads, context.md, saved browser
#  logins, logs and .env are yours, and an uninstaller is the last place that
#  should be making that decision for you — pass --purge if you really want
#  them gone.
#
#  Options (also readable as environment variables, for `curl | bash`):
#      --purge   FREECLAW_PURGE=1   also delete .env, Flask/static, logs,
#                                   browser-profiles. Irreversible.
#      --yes     FREECLAW_YES=1     don't ask for confirmation.
#      --dir     FREECLAW_DIR=...   which install to remove.
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

die() { echo ""; error "$1"; echo ""; exit 1; }

INSTALL_DIR="${FREECLAW_DIR:-}"
PURGE="${FREECLAW_PURGE:-}"
YES="${FREECLAW_YES:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)   INSTALL_DIR="$2"; shift 2 ;;
        --purge) PURGE=1; shift ;;
        --yes|-y) YES=1; shift ;;
        -h|--help)
            sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

# A copy of this script ships inside every install, so if it is sitting in
# one, that is the install to remove — whatever directory it happens to be in.
# Someone who installed to /opt/freeclaw should not have to say so.
if [[ -z "$INSTALL_DIR" ]]; then
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    if [[ -f "$here/.freeclaw-install" ]]; then
        INSTALL_DIR="$here"
    else
        INSTALL_DIR="$HOME/.freeclaw"
    fi
fi

echo ""
echo -e "   ${RED}${BOLD}FreeClaw Uninstaller${RESET} ${GRAY}(macOS)${RESET}"
echo ""
divider
echo ""

[[ -d "$INSTALL_DIR" ]] || die "No FreeClaw install at ${INSTALL_DIR}."

# One explicit marker, written by install-mac.sh and gitignored, rather than
# guessing from the contents. Guessing does not work here: an install *is* a
# clone of the repo, so it has .git and src/ exactly like a developer's
# checkout does — and deleting src/ and Flask/ out of somebody's working tree
# would be a spectacular own goal.
if [[ ! -f "$INSTALL_DIR/.freeclaw-install" ]]; then
    error "${INSTALL_DIR} has no .freeclaw-install marker."
    info "That file is what tells a FreeClaw install apart from a checkout of"
    info "the repo, and this refuses to delete the latter. If this really is"
    info "your install, point at it explicitly:"
    info "  ${LIME}./uninstall-mac.sh --dir '${INSTALL_DIR}'${RESET}  after creating the marker,"
    info "or remove the folder by hand."
    echo ""
    exit 1
fi

# ── Confirm ──────────────────────────────────

if [[ -z "$YES" ]]; then
    echo -e "     ${GRAY}About to remove FreeClaw from:${RESET}"
    echo -e "     ${BOLD}${INSTALL_DIR}${RESET}"
    echo ""
    if [[ -n "$PURGE" ]]; then
        warn "--purge: your chats, uploads, context.md, saved browser logins,"
        warn "logs and .env go too. This cannot be undone."
    else
        info "Your chats, uploads, .env and logs will be left in place."
    fi
    echo ""
    read -r -p "$(echo -e "     ${LIME}?${RESET}  Type ${BOLD}yes${RESET} to continue: ")" confirm < /dev/tty
    echo ""
    [[ "$confirm" == "yes" ]] || { info "Uninstall cancelled."; echo ""; exit 0; }
    divider
    echo ""
fi

# ── Stop FreeClaw ────────────────────────────

step "1" "Stopping FreeClaw..."
echo ""

# By pid, and only after checking what that pid actually is: an install path is
# user-controlled, so matching on it is a quoting hazard, and a stale pid file
# can name a process that has since been recycled.
stop_pid() {
    local file="$1" pattern="$2" pid
    [[ -f "$file" ]] || return 0
    pid=$(head -n1 "$file" 2>/dev/null | tr -dc '0-9')
    [[ -n "$pid" ]] || { rm -f "$file"; return 0; }
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "$pattern"; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            ps -p "$pid" &>/dev/null || break
            sleep 0.5
        done
        ps -p "$pid" &>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
}

# The menu bar app first: it is the supervisor, and it stops its own server on
# the way out. The second call is for the case where it didn't get the chance.
stop_pid "$INSTALL_DIR/freeclaw.pid" "tray.py"
stop_pid "$INSTALL_DIR/freeclaw-server.pid" "Flask.main"
rm -f "$INSTALL_DIR/freeclaw.lock"
success "Stopped"

echo ""
divider
echo ""

# ── App, login item, CLI ─────────────────────

step "2" "Removing the app, the login item and the CLI..."
echo ""

# Every one of these is removed only if it actually points at the install
# being removed. A second FreeClaw somewhere else is left alone.
points_here() { [[ -f "$1" ]] && grep -qF "$INSTALL_DIR" "$1"; }

APP_BUNDLE="$HOME/Applications/FreeClaw.app"
if [[ -d "$APP_BUNDLE" ]]; then
    if points_here "$APP_BUNDLE/Contents/MacOS/FreeClaw"; then
        rm -rf "$APP_BUNDLE"
        success "FreeClaw.app removed from ~/Applications"
    else
        info "left ~/Applications/FreeClaw.app alone — it points at another install"
    fi
fi

LAUNCH_AGENT="$HOME/Library/LaunchAgents/dev.eedeb.freeclaw.plist"
if [[ -f "$LAUNCH_AGENT" ]]; then
    if points_here "$LAUNCH_AGENT"; then
        # No `launchctl bootout`: the job it names is the process this script
        # just stopped, and the plist is only read at login anyway.
        rm -f "$LAUNCH_AGENT"
        success "Login item removed"
    else
        info "left the login item alone — it points at another install"
    fi
fi

for cli in /usr/local/bin/freeclaw "$HOME/.local/bin/freeclaw"; do
    [[ -f "$cli" ]] || continue
    if ! points_here "$cli"; then
        info "left ${cli} alone — it points at another install"
        continue
    fi
    if rm -f "$cli" 2>/dev/null || sudo rm -f "$cli" 2>/dev/null; then
        success "Removed ${cli}"
    else
        warn "Could not remove ${cli} — remove it by hand"
    fi
done

echo ""
divider
echo ""

# ── Files ────────────────────────────────────

step "3" "Removing files..."
echo ""

cd "$INSTALL_DIR"

# uninstall-mac.sh is kept alongside the data it leaves behind: without it
# there is nothing in the directory that can finish the job, and --purge later
# would mean fetching the script again.
KEEP=(".env" "logs" "browser-profiles" "uninstall-mac.sh" ".freeclaw-install")

if [[ -n "$PURGE" ]]; then
    # Everything except this script first, so a real problem is reported
    # rather than hidden behind the self-delete at the end.
    shopt -s dotglob nullglob
    for item in *; do
        [[ "$item" == "uninstall-mac.sh" ]] && continue
        rm -rf "$item" 2>/dev/null || warn "couldn't remove ${item}"
    done
    shopt -u dotglob nullglob
    success "Everything removed"
else
    shopt -s dotglob nullglob
    for item in *; do
        # Flask holds both program files (main.py, templates/) and the user's
        # entire history (static/), so it is the one directory walked into
        # rather than kept or deleted whole.
        if [[ "$item" == "Flask" ]]; then
            for sub in Flask/*; do
                [[ "$(basename "$sub")" == "static" ]] && continue
                rm -rf "$sub" 2>/dev/null || true
            done
            continue
        fi
        keep=""
        for k in "${KEEP[@]}"; do [[ "$item" == "$k" ]] && keep=1; done
        [[ -n "$keep" ]] && continue
        rm -rf "$item" 2>/dev/null || warn "couldn't remove ${item}"
    done
    shopt -u dotglob nullglob
    success "Program files removed"
fi

echo ""
divider
echo ""

echo -e "   ${LIME}${BOLD}FreeClaw has been uninstalled.${RESET}"
echo ""
if [[ -z "$PURGE" ]]; then
    echo -e "   ${GRAY}Your data is still in:${RESET}"
    echo -e "     ${BOLD}${INSTALL_DIR}${RESET}"
    echo ""
    echo -e "   ${GRAY}Delete that folder to remove it, or finish the job with:${RESET}"
    echo -e "     ${GRAY}${INSTALL_DIR}/uninstall-mac.sh --purge${RESET}"
    echo ""
fi
echo -e "   ${GRAY}Thanks for using FreeClaw — hope to see you again.${RESET}"
echo ""

# A running script can't safely delete the file it is being read from — bash
# reads a script in chunks as it goes. `exec` hands the process over to rm, so
# by the time the file disappears there is nothing left that needs to read it.
if [[ -n "$PURGE" ]]; then
    cd /
    exec rm -rf "$INSTALL_DIR"
fi
