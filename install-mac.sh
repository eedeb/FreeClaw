#!/bin/bash
# pipefail matters here: git and pip output is piped through indent, and
# without it a failed clone would be masked by sed's exit status and the
# script would sail on to report success.
set -eo pipefail

# ─────────────────────────────────────────────
#  FreeClaw — macOS Installer
#  github.com/eedeb/FreeClaw
#
#  curl -fsSL https://freeclaw.eedeb.dev/install-mac.sh | bash
#
#  A native install: no Docker, no Homebrew, no Python of your own. The tree
#  goes into ~/.freeclaw with a private interpreter beside it, and FreeClaw
#  runs as a menu bar app (mac/tray.py) that supervises the server — the same
#  shape install.ps1 gives Windows, where the notification-area app plays the
#  part systemd plays on Linux.
#
#      ~/.freeclaw
#        ├── python/        private interpreter + deps
#        ├── Flask/ src/ models/   the clone
#        ├── mac/tray.py    the menu bar app
#        ├── logs/
#        ├── .env           merged, never clobbered
#        └── .freeclaw-install   marks this as an install
#
#  Nothing is published or hosted for this to work: the source comes from
#  GitHub and the interpreter from the python-build-standalone project, so
#  there is no release to cut and no artifact to upload. Re-running it is the
#  update path.
#
#  Piped through `bash` there is nowhere to put flags, so every option also
#  reads an environment variable — see "Options" below.
# ─────────────────────────────────────────────

REPO_URL="https://github.com/eedeb/FreeClaw"

# The private interpreter. python-build-standalone publishes relocatable
# CPython builds for macOS; this is the exact counterpart of the embeddable
# distribution install.ps1 fetches from python.org, and it plays the part
# install.sh's virtualenv plays on Linux.
#
# 3.12 and not Apple's own python3, which is 3.9: requirements.txt gates the
# shipped browser MCP server on `python_version >= "3.10"`, so a 3.9 install
# would silently come up without it.
#
# The digests are pinned rather than fetched alongside the download — a
# checksum served by the host you are downloading from only proves the file
# arrived intact. Update all three together when moving the version.
PBS_RELEASE="20260901"
PY_VERSION="3.12.14"
PY_SHA256_ARM64="3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76"
PY_SHA256_X86_64="2e31b23f3f1319f707d0e620b48847a0046577541d357276821f9f1b5492e0ba"

# ── Options ──────────────────────────────────

INSTALL_DIR="${FREECLAW_DIR:-$HOME/.freeclaw}"
BRANCH="${FREECLAW_BRANCH:-main}"
# FREECLAW_PASSWORD is for unattended installs only. Deliberately not a flag:
# a command line is readable by anything that can list processes.
PASSWORD="${FREECLAW_PASSWORD:-}"
NO_START="${FREECLAW_NO_START:-}"
NO_APP="${FREECLAW_NO_APP:-}"
NO_CLI="${FREECLAW_NO_CLI:-}"
AUTOSTART="${FREECLAW_AUTOSTART:-}"      # unset = ask; 0/1 = decided

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)          INSTALL_DIR="$2"; shift 2 ;;
        --branch)       BRANCH="$2"; shift 2 ;;
        --no-start)     NO_START=1; shift ;;
        --no-app)       NO_APP=1; shift ;;
        --no-cli)       NO_CLI=1; shift ;;
        --autostart)    AUTOSTART=1; shift ;;
        --no-autostart) AUTOSTART=0; shift ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1 ;;
    esac
done

APP_BUNDLE="$HOME/Applications/FreeClaw.app"

# Top-level paths another platform's install owns. Excluded from the checkout
# so a Mac install doesn't carry systemd scripts and Windows batch files it
# can never run. install.sh does the mirror image of this.
OTHER_PLATFORMS=(
    "/install.sh"
    "/update.sh"
    "/uninstall.sh"
    "/install.ps1"
    "/uninstall.ps1"
    "/windows/"
)

# Development-only paths, skipped on every platform: the benchmark harness is
# run against an install rather than by it, and the telemetry collector is
# deployed to Cloudflare, not executed here. Keep in sync with install.sh.
DEV_ONLY=(
    "/bench/"
    "/telemetry/"
)

# Refreshed from the repo on an upgrade, path by path — never a bare checkout.
# Flask/static is where every chat, upload and context.md lives, and the Setup
# Wizard inside it is a *tracked* folder that becomes a live user the moment
# somebody talks to it, so `git checkout .` would reset their conversation.
# Same reasoning as update.sh on Linux and install.ps1 on Windows.
UPGRADE_PATHS=(
    "src" "models" "mac" "Flask/main.py" "Flask/templates"
    "requirements.txt" "VERSION" "update-mac.sh" "uninstall-mac.sh"
)

# Colors & styles
RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"

LIME="\033[38;5;154m"       # #c8f04a-ish (256-color lime)
WHITE="\033[0;97m"
GRAY="\033[0;90m"
RED="\033[0;31m"
YELLOW="\033[0;33m"

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
    echo -e "   ${DIM}${GRAY}github.com/eedeb/FreeClaw  ·  macOS${RESET}"
    echo ""
    echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"
    echo ""
}

step() {
    echo -e "   ${BG_DARK} ${LIME}${BOLD}${1}${RESET}${BG_DARK} ${RESET} ${BOLD}${WHITE}${2}${RESET}"
}

info()    { echo -e "     ${GRAY}→${RESET}  $1"; }
success() { echo -e "     ${LIME}✓${RESET}  $1"; }
warn()    { echo -e "     ${YELLOW}!${RESET}  $1"; }
error()   { echo -e "     ${RED}✗${RESET}  $1"; }
section_gap() { echo ""; }
divider() { echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"; }

indent() { sed 's/^/       /'; }

die() { error "$1"; section_gap; exit 1; }

# Is there a human on the other end? `curl … | bash` leaves stdin as the pipe,
# so every prompt reads /dev/tty instead — and if there isn't one (CI, a
# provisioning script), the install has to finish on its own rather than
# hanging forever on a password prompt nobody can see.
# In a subshell, and opened rather than merely tested: /dev/tty exists even
# where it cannot be opened (a launchd job, a CI runner), and bash applies the
# redirections of an `exec` before the `2>/dev/null` that would have silenced
# its complaint — so the failure has to happen somewhere its stderr is already
# pointed away, and somewhere a leftover file descriptor cannot survive.
have_tty() { ( : < /dev/tty ) 2>/dev/null; }
if have_tty; then HAS_TTY=1; else HAS_TTY=""; fi

ask() {
    # ask <prompt> <variable>; silent with -s as the third argument.
    local prompt="$1" var="$2" silent="$3"
    if [[ "$silent" == "-s" ]]; then
        read -r -s -p "$(echo -e "$prompt")" "$var" < /dev/tty
        echo ""
    else
        read -r -p "$(echo -e "$prompt")" "$var" < /dev/tty
    fi
}

# ── Preflight ────────────────────────────────

print_banner

step "0" "Checking prerequisites..."
section_gap

if [[ "$(uname -s)" != "Darwin" ]]; then
    error "This is the macOS installer, but this machine is $(uname -s)."
    info "On Linux use the native installer instead:"
    info "  ${LIME}curl -fsSL https://freeclaw.eedeb.dev/install.sh | bash${RESET}"
    exit 1
fi
success "macOS $(sw_vers -productVersion 2>/dev/null || echo "") detected"

case "$(uname -m)" in
    arm64)  PY_ARCH="aarch64-apple-darwin"; PY_SHA256="$PY_SHA256_ARM64" ;;
    x86_64) PY_ARCH="x86_64-apple-darwin";  PY_SHA256="$PY_SHA256_X86_64" ;;
    *)      die "Unsupported architecture: $(uname -m)." ;;
esac
success "$(uname -m) build selected"

for cmd in git curl shasum tar; do
    command -v "$cmd" &>/dev/null || {
        error "${cmd} is required but not found."
        info "Install Apple's command line tools with: ${LIME}xcode-select --install${RESET}"
        exit 1
    }
done
success "git, curl and tar found"

IS_UPGRADE=""
[[ -d "$INSTALL_DIR/.git" ]] && IS_UPGRADE=1

# FreeClaw used to run on macOS in a container, because there is no systemd
# here and a supervisor had to come from somewhere. It doesn't any more — this
# installer is the replacement — but an older install is still sitting there
# with `restart: unless-stopped`, which means it is holding port 6767 and will
# take it again at every boot. Nothing is removed on the user's behalf: that
# container is the only copy of their old chats.
if command -v docker &>/dev/null && docker info &>/dev/null \
   && [[ -n "$(docker ps -aq --filter 'name=^/freeclaw$' 2>/dev/null)" ]]; then
    section_gap
    warn "An older Docker-based FreeClaw is still installed on this machine."
    info "It holds port 6767, so this install can't start while it runs."
    info "Stop and remove it with:"
    info "  ${LIME}docker rm -f freeclaw${RESET}"
    info "  ${LIME}docker rmi freeclaw:local${RESET}"
    info "Your old chats are in that install's ${BOLD}Flask/static${RESET} folder on disk —"
    info "copy anything you want to keep into ${BOLD}${INSTALL_DIR}/Flask/static${RESET} afterwards."
fi

section_gap
divider
section_gap

# ── Stop a running FreeClaw ──────────────────

# mac/tray.py writes both pids at startup. Killing by pid rather than by
# matching process paths keeps this free of quoting hazards — an install path
# is user-controlled — and the command check below is the safety net against a
# stale pid file naming a recycled pid.
stop_pid() {
    local file="$1" pattern="$2" signal="${3:-TERM}" pid
    [[ -f "$file" ]] || return 0
    pid=$(head -n1 "$file" 2>/dev/null | tr -dc '0-9')
    [[ -n "$pid" ]] || return 0
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "$pattern"; then
        kill "-$signal" "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            ps -p "$pid" &>/dev/null || break
            sleep 0.5
        done
        ps -p "$pid" &>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
}

if [[ -f "$INSTALL_DIR/freeclaw.pid" || -f "$INSTALL_DIR/freeclaw-server.pid" ]]; then
    step "1" "Stopping the running FreeClaw..."
    section_gap
    # The menu bar app first: it is the supervisor, and it stops its own
    # server on the way out. The second call is for the case where it didn't
    # get the chance.
    stop_pid "$INSTALL_DIR/freeclaw.pid" "tray.py"
    stop_pid "$INSTALL_DIR/freeclaw-server.pid" "Flask.main"
    success "Stopped"
    section_gap
    divider
    section_gap
fi

# ── Source ───────────────────────────────────

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [[ -n "$IS_UPGRADE" ]]; then
    step "2" "Updating the source..."
    section_gap
    info "Fetching from ${REPO_URL}"
    git fetch --depth 1 origin "$BRANCH" 2>&1 | indent \
        || die "git fetch failed — no network, or the repo moved."
    for path in "${UPGRADE_PATHS[@]}"; do
        git checkout "origin/$BRANCH" -- "$path" 2>/dev/null || true
    done
    # Move HEAD so `git log` is honest next time, without touching the tree.
    git reset --soft "origin/$BRANCH" 2>/dev/null || true
    success "Source updated (your chats, .env and logs untouched)"
else
    step "2" "Cloning the repository..."
    section_gap
    info "Fetching from ${REPO_URL}"
    # init + fetch rather than `git clone`, because the directory may already
    # exist — someone made it first, or an older install left files here.
    # clone refuses a non-empty target; this does not.
    git init -q
    git remote add origin "$REPO_URL" 2>/dev/null \
        || git remote set-url origin "$REPO_URL"
    git fetch --depth 1 origin "$BRANCH" 2>&1 | indent \
        || die "Couldn't fetch ${REPO_URL} — check your network."

    # Check out everything except the other platforms' files. Non-cone mode is
    # what allows negated patterns; it needs git 2.25+, so fall back to
    # deleting the files after a normal checkout on anything older.
    if git sparse-checkout init --no-cone &>/dev/null; then
        {
            echo '/*'
            for path in "${OTHER_PLATFORMS[@]}" "${DEV_ONLY[@]}"; do echo "!${path}"; done
        } | git sparse-checkout set --stdin
        git checkout -f -B "$BRANCH" "origin/$BRANCH" 2>&1 | indent \
            || die "git checkout failed."
        success "Repository ready (other platforms' files skipped)"
    else
        warn "git is too old for sparse-checkout — pruning after checkout instead"
        git checkout -f -B "$BRANCH" "origin/$BRANCH" 2>&1 | indent \
            || die "git checkout failed."
        for path in "${OTHER_PLATFORMS[@]}" "${DEV_ONLY[@]}"; do rm -rf ".${path}"; done
        success "Repository ready (other platforms' files removed)"
    fi
fi

VERSION="unknown"
[[ -f VERSION ]] && VERSION=$(tr -d '[:space:]' < VERSION)

# Created up front so nothing has to guess whether they exist later.
mkdir -p Flask/static logs

section_gap
divider
section_gap

# ── Python ───────────────────────────────────

PY="$INSTALL_DIR/python/bin/python3"

step "3" "Setting up Python..."
section_gap

if [[ -x "$PY" ]]; then
    info "using the Python already in $INSTALL_DIR/python"
    success "Python $("$PY" -c 'import platform; print(platform.python_version())')"
else
    tarball="cpython-${PY_VERSION}+${PBS_RELEASE}-${PY_ARCH}-install_only.tar.gz"
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${tarball}"
    tmp=$(mktemp -d)
    # Clean up the download whichever way this ends — including a checksum
    # failure, which exits from inside the trap's reach.
    trap 'rm -rf "$tmp"' EXIT

    info "Downloading Python ${PY_VERSION} (private to this install)"
    curl -fsSL "$url" -o "$tmp/$tarball" \
        || die "Couldn't download Python from ${url}"

    actual=$(shasum -a 256 "$tmp/$tarball" | awk '{print $1}')
    if [[ "$actual" != "$PY_SHA256" ]]; then
        error "The Python download doesn't match its pinned checksum."
        info "expected  ${PY_SHA256}"
        info "got       ${actual}"
        die "Refusing to install it."
    fi
    success "Download verified"

    # The archive's top-level directory is "python", so this lands exactly
    # where $PY expects it.
    tar -xzf "$tmp/$tarball" -C "$INSTALL_DIR" || die "Couldn't unpack the Python archive."
    [[ -x "$PY" ]] || die "The Python archive didn't contain python/bin/python3."
    rm -rf "$tmp"
    trap - EXIT
    success "Python ${PY_VERSION} installed privately (your own Python is untouched)"
fi

section_gap
divider
section_gap

# ── Dependencies ─────────────────────────────

step "4" "Installing dependencies..."
section_gap
info "this is the slow part on a first install"

if ! "$PY" -m pip install --no-cache-dir --disable-pip-version-check -q \
        -r requirements.txt -r mac/requirements-mac.txt 2>&1 | indent; then
    die "pip install failed — see the errors above."
fi
success "Dependencies installed"

# models/run_model.py asks for this table on import, so an install that later
# runs offline would lose the intent classifier on every turn. Never fatal:
# run_model.py falls back to fetching it on first use.
if "$PY" -c "import nltk; nltk.data.find('tokenizers/punkt_tab')" &>/dev/null; then
    success "Classifier data already present"
elif "$PY" -c "import nltk; nltk.download('punkt_tab', quiet=True)" &>/dev/null; then
    success "Classifier data downloaded"
else
    warn "Couldn't fetch the NLTK data; Classy will retry on first use."
fi

section_gap
divider
section_gap

# ── Configuration ────────────────────────────

step "5" "Configuration..."
section_gap

# Merge, never clobber. On an upgrade .env is not a file this installer wrote:
# by then it holds the user's providers, their MCP servers, their install_id
# and whatever they typed into Settings. Every key below is written only if it
# is absent, and nothing is ever removed.
env_has() { [[ -f .env ]] && grep -qE "^[[:space:]]*${1}=" .env; }

env_append() {
    # Under umask 077 so a password is never briefly world-readable.
    (
        umask 077
        # A hand-edited .env may not end in a newline, and joining two keys
        # onto one line would break both.
        if [[ -s .env ]] && [[ -n "$(tail -c 1 .env)" ]]; then echo "" >> .env; fi
        printf '%s=%s\n' "$1" "$2" >> .env
    )
}

GENERATED_PASSWORD=""

if env_has FC_PASSWORD; then
    success "Password kept from the existing .env"
elif [[ -n "$PASSWORD" ]]; then
    env_append FC_PASSWORD "$PASSWORD"
    success "Password taken from FREECLAW_PASSWORD"
elif [[ -n "$HAS_TTY" ]]; then
    echo -e "     ${GRAY}Just a login password now — you'll add your AI provider(s)${RESET}"
    echo -e "     ${GRAY}from the web UI after install (no API keys needed here).${RESET}"
    section_gap
    while true; do
        ask "     ${LIME}?${RESET}  Set the Web UI password: " fc_password -s
        ask "     ${LIME}?${RESET}  Confirm password: " fc_password_confirm -s
        if [[ -z "$fc_password" ]]; then
            warn "Password can't be empty — please try again."
            section_gap
        elif [[ "$fc_password" != "$fc_password_confirm" ]]; then
            warn "Passwords do not match — please try again."
            section_gap
        else
            break
        fi
    done
    env_append FC_PASSWORD "$fc_password"
    success "Password saved"
else
    # No human to ask. Inventing one beats leaving the web UI unprotected —
    # and it is printed at the end, which is the only way back in.
    GENERATED_PASSWORD=$("$PY" - <<'EOF'
import secrets
# Ambiguous glyphs removed: this gets read off a terminal and typed into a
# browser, and l/I/1 and O/0 are where that goes wrong.
alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
print("".join(secrets.choice(alphabet) for _ in range(16)))
EOF
)
    env_append FC_PASSWORD "$GENERATED_PASSWORD"
    success "Password generated (printed at the end)"
fi

if env_has SECRET_KEY; then
    success "Session secret kept"
else
    # Without it Flask generates a throwaway per boot and every restart logs
    # everyone out.
    env_append SECRET_KEY "$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
    success "Session secret generated"
fi

# ── Telemetry opt-in ─────────────────────────
# Default is no: anything other than an explicit "y" leaves it off, and the
# empty answer from just hitting Enter lands there too.
if env_has FC_TELEMETRY; then
    :
elif [[ -n "$HAS_TTY" ]]; then
    section_gap
    echo -e "     ${GRAY}Optional: send one anonymous ping so I can count installs.${RESET}"
    echo -e "     ${GRAY}It contains a random ID, the FreeClaw version, your OS, and${RESET}"
    echo -e "     ${GRAY}the word \"native\". That's the whole payload — no chats, no${RESET}"
    echo -e "     ${GRAY}prompts, no API keys, no provider names.${RESET}"
    echo -e "     ${GRAY}Sent once, never repeated. Off by default, and you can flip${RESET}"
    echo -e "     ${GRAY}it either way later in ${BOLD}Settings${RESET}${GRAY}.${RESET}"
    section_gap
    ask "     ${LIME}?${RESET}  Send the install ping? [y/${BOLD}N${RESET}]: " fc_telemetry_answer
    case "$fc_telemetry_answer" in
        [Yy]*) env_append FC_TELEMETRY 1; success "Anonymous install ping enabled — thank you" ;;
        *)     env_append FC_TELEMETRY 0; success "Telemetry off" ;;
    esac
else
    env_append FC_TELEMETRY 0
fi

chmod 600 .env 2>/dev/null || true

# The marker uninstall-mac.sh keys off. An install is a clone of the repo, so
# it looks exactly like a developer's checkout from the outside — this file is
# the only thing that tells them apart. Gitignored, so a checkout never has one.
{
    echo "# Written by install-mac.sh. Its presence marks this directory as a"
    echo "# FreeClaw install rather than a source checkout; uninstall-mac.sh"
    echo "# looks for it."
    echo "version=${VERSION}"
    echo "installed=$(date +%Y-%m-%dT%H:%M:%S)"
} > .freeclaw-install

chmod +x update-mac.sh uninstall-mac.sh 2>/dev/null || true

section_gap
divider
section_gap

# ── The app ──────────────────────────────────

step "6" "Installing the FreeClaw app..."
section_gap

if [[ -n "$NO_APP" ]]; then
    info "Skipped (--no-app)"
else
    # A real .app bundle, in ~/Applications so it needs no administrator
    # rights, so Spotlight and Launchpad find it, and so "FreeClaw" is what
    # macOS calls it rather than "python3". The executable inside is a two
    # line shell script: there is nothing to compile, and the bundle exists
    # for its Info.plist and its icon.
    rm -rf "$APP_BUNDLE"
    mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"

    cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>FreeClaw</string>
    <key>CFBundleDisplayName</key><string>FreeClaw</string>
    <key>CFBundleIdentifier</key><string>dev.eedeb.freeclaw</string>
    <key>CFBundleExecutable</key><string>FreeClaw</string>
    <key>CFBundleIconFile</key><string>freeclaw</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <!-- The menu bar is the whole interface: no Dock tile, no app menu.
         mac/tray.py sets the same policy at runtime, for the times it is
         started without going through this bundle. -->
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

    cat > "$APP_BUNDLE/Contents/MacOS/FreeClaw" <<LAUNCHER
#!/bin/bash
# Launcher for the FreeClaw menu bar app. Written by install-mac.sh with the
# install location baked in; reinstalling rewrites it.
exec "${INSTALL_DIR}/python/bin/python3" "${INSTALL_DIR}/mac/tray.py" "\$@"
LAUNCHER
    chmod +x "$APP_BUNDLE/Contents/MacOS/FreeClaw"
    cp mac/freeclaw.icns "$APP_BUNDLE/Contents/Resources/freeclaw.icns"

    # Bump the bundle's mtime and tell Launch Services about it, so Finder
    # picks up a changed icon or version instead of showing the cached one.
    touch "$APP_BUNDLE"
    lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    [[ -x "$lsregister" ]] && "$lsregister" -f "$APP_BUNDLE" &>/dev/null || true

    success "FreeClaw.app installed to ~/Applications"
fi

section_gap
divider
section_gap

# ── CLI ──────────────────────────────────────

step "7" "Installing the freeclaw CLI..."
section_gap

if [[ -n "$NO_CLI" ]]; then
    info "Skipped (--no-cli)"
else
    # Absolute paths baked in rather than derived from $0: the shim is
    # installed outside the tree it points at, and resolving a symlink back to
    # its target portably is more moving parts than an install location that
    # is already known here.
    cli_body="#!/bin/bash
# FreeClaw CLI — the same conversation as the web UI, from any terminal.
# Written by install-mac.sh. cd first: src/cli.py builds paths into
# Flask/static from the working directory.
cd \"${INSTALL_DIR}\" || exit 1
exec \"${INSTALL_DIR}/python/bin/python3\" -m src.cli \"\$@\"
"
    write_cli() { printf '%s' "$cli_body" > "$1" 2>/dev/null && chmod +x "$1" 2>/dev/null; }

    # A plain write first; then sudo (which prompts on /dev/tty, so it still
    # works under `curl | bash`); then ~/.local/bin if the user would rather
    # not give a password. Failing to install the CLI is never fatal — the web
    # UI is unaffected.
    cli_target="/usr/local/bin/freeclaw"
    if write_cli "$cli_target"; then
        success "CLI installed — run 'freeclaw' from anywhere"
    elif [[ -n "$HAS_TTY" ]] \
        && info "Writing to /usr/local/bin needs administrator access" \
        && sudo mkdir -p /usr/local/bin 2>/dev/null \
        && printf '%s' "$cli_body" | sudo tee "$cli_target" > /dev/null 2>&1 \
        && sudo chmod +x "$cli_target" 2>/dev/null; then
        success "CLI installed — run 'freeclaw' from anywhere"
    else
        cli_target="$HOME/.local/bin/freeclaw"
        mkdir -p "$HOME/.local/bin"
        if write_cli "$cli_target"; then
            success "CLI installed to ~/.local/bin/freeclaw"
            warn "Add ~/.local/bin to your PATH to run it by name"
        else
            warn "Could not install the freeclaw CLI — skipping (the web UI is unaffected)"
        fi
    fi
fi

section_gap
divider
section_gap

# ── Start at login ───────────────────────────

step "8" "Starting FreeClaw when you log in..."
section_gap

if [[ -z "$AUTOSTART" ]]; then
    if [[ -n "$HAS_TTY" ]]; then
        ask "     ${LIME}?${RESET}  Start FreeClaw automatically at login? [${BOLD}Y${RESET}/n]: " autostart_answer
        case "$autostart_answer" in
            [Nn]*) AUTOSTART=0 ;;
            *)     AUTOSTART=1 ;;
        esac
    else
        AUTOSTART=0
    fi
fi

if [[ "$AUTOSTART" == "1" ]]; then
    # Through tray.py rather than writing the plist here, so the installer and
    # the app's own "Start at Login" menu item can never disagree about what
    # the LaunchAgent should contain.
    if "$PY" -c "import sys; sys.path.insert(0, '${INSTALL_DIR}/mac'); import tray; tray.set_autostart(True)" 2>/dev/null; then
        success "FreeClaw will start when you log in"
        info "turn it off any time from the menu bar icon"
    else
        warn "Couldn't write the LaunchAgent — enable it from the menu bar icon instead"
    fi
else
    info "Not enabled — you can switch it on from the menu bar icon"
fi

section_gap
divider
section_gap

# ── Providers ────────────────────────────────

step "9" "AI providers & MCP servers..."
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

# ── Start ────────────────────────────────────

READY=""
if [[ -z "$NO_START" ]]; then
    step "10" "Starting FreeClaw..."
    section_gap

    if [[ -n "$NO_APP" ]]; then
        # No bundle to open, so launch the app directly and detach it — this
        # script is about to exit, and the menu bar app must outlive it.
        nohup "$PY" "$INSTALL_DIR/mac/tray.py" >/dev/null 2>&1 &
        disown 2>/dev/null || true
    else
        # Never fatal: `open` fails over SSH and in any session without a
        # window server, and an install that is otherwise complete should say
        # so rather than abort on its last step.
        open "$APP_BUNDLE" || warn "Couldn't launch the app — open FreeClaw from ~/Applications."
    fi

    info "waiting for the web UI (the first start loads the classifier)..."
    for _ in $(seq 1 90); do
        if curl -fsS -o /dev/null "http://127.0.0.1:6767/login" 2>/dev/null; then
            READY=1
            break
        fi
        sleep 2
    done

    if [[ -n "$READY" ]]; then
        success "FreeClaw is running"
    else
        warn "It didn't answer within three minutes."
        info "Check ${LIME}${INSTALL_DIR}/logs/tray.log${RESET}"
    fi

    section_gap
    divider
    section_gap
fi

# ── Done ─────────────────────────────────────

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)

if [[ -n "$IS_UPGRADE" ]]; then
    echo -e "   ${LIME}${BOLD}Updated to FreeClaw ${VERSION}.${RESET}"
    echo -e "   ${GRAY}Your chats, files and settings were left untouched.${RESET}"
else
    echo -e "   ${LIME}${BOLD}FreeClaw ${VERSION} is installed.${RESET}"
fi
echo ""
echo -e "   ${GRAY}Open the web UI in your browser:${RESET}"
echo ""
echo -e "   ${BG_DARK}   ${LIME}${BOLD}http://127.0.0.1:6767${RESET}${BG_DARK}   ${RESET}"
echo ""
if [[ -n "$GENERATED_PASSWORD" ]]; then
    echo -e "   ${GRAY}Password:${RESET}  ${LIME}${BOLD}${GENERATED_PASSWORD}${RESET}"
    echo -e "   ${DIM}${GRAY}(nobody chose this one — change it in Settings → Environment)${RESET}"
    echo ""
fi
if [[ -n "$LAN_IP" ]]; then
    echo -e "   ${DIM}${GRAY}From other devices on your network: http://${LAN_IP}:6767${RESET}"
    echo ""
fi
echo -e "   ${YELLOW}First step:${RESET} ${GRAY}open ${BOLD}⚙ Settings → Providers${RESET}${GRAY} and add an AI provider —${RESET}"
echo -e "   ${GRAY}FreeClaw can't answer until at least one is configured.${RESET}"
echo ""
echo -e "   ${DIM}${GRAY}The built-in OpenAI-compatible API is available at:${RESET}"
echo -e "   ${DIM}${GRAY}  http://127.0.0.1:6767/v1  (toggle on/off from the homepage)${RESET}"
echo -e "   ${DIM}${GRAY}  Use your FreeClaw password as the Bearer token.${RESET}"
echo ""
echo -e "   ${DIM}${GRAY}Menu bar    ${RESET}${GRAY}the claw at the top right — status, restart, logs${RESET}"
echo -e "   ${DIM}${GRAY}Terminal    ${RESET}${LIME}${BOLD}freeclaw${RESET}"
echo -e "   ${DIM}${GRAY}Update      ${RESET}${GRAY}Settings → Update FreeClaw, or ${INSTALL_DIR}/update-mac.sh${RESET}"
echo -e "   ${DIM}${GRAY}Uninstall   ${RESET}${GRAY}${INSTALL_DIR}/uninstall-mac.sh${RESET}"
echo -e "   ${DIM}${GRAY}Logs        ${RESET}${GRAY}${INSTALL_DIR}/logs/${RESET}"
echo ""
divider
echo ""
