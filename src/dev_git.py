"""Git for the Developer app: clone a GitHub repo into the projects folder,
then status, diff, commit, pull, push and branches.

The token is the one the person gave FreeClaw's GitHub connection (App Store
→ GitHub); mcp_client hands it to the Developer server as
FC_DEV_GITHUB_TOKEN. It reaches git through GIT_CONFIG_* variables (an
Authorization header for https://github.com/ only), never a command line or
a file, and it's never put in the environment of code the Run button or the
agent's `run` starts (dev_server.spawn drops it).

A repo can tell git to run commands of its own (hooks, filters, fsmonitor, a
pager, an external diff), and code in the workspace can edit a repo's
.git/config. So every git here runs with hooks off and those settings forced
off on the command line, which outranks the repo's config. Hosting goes
further and runs git inside its sandbox (git_process is the hook for that).
"""
import base64
import contextlib
import contextvars
import json
import os
import re
import subprocess
import urllib.request

TIMEOUT = 120
OUTPUT_MAX = 6000
DIFF_MAX = 8000

# Only GitHub, only https. owner/repo, optionally .git.
_REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

# Forced on every git call: repo config can't turn these back on.
SAFE_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.pager=cat",
    "-c", "core.sshCommand=false",
    "-c", "core.askPass=",
    "-c", "credential.helper=",
    "-c", "diff.external=",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "submodule.recurse=false",
    "-c", "safe.directory=*",
    # FreeClaw takes the execute bit off every file under Flask/static, the
    # projects folder included, at startup and as it runs (uploads.harden_tree).
    # Without this, every restart would show a repo's scripts as changed
    # (100755 -> 100644, nothing else). Ignored, a file keeps the mode it has on
    # GitHub; scripts still run, as `bash file.sh` doesn't need the bit.
    "-c", "core.fileMode=false",
]


class GitError(Exception):
    pass


# The web app's own git calls (the Developer app's buttons) pass the person's
# token for the request; the Developer server's come from its environment.
_TOKEN = contextvars.ContextVar("fc_dev_github_token", default=None)


def token():
    t = _TOKEN.get()
    return (t if t is not None else os.environ.get("FC_DEV_GITHUB_TOKEN") or "").strip()


@contextlib.contextmanager
def using_token(t):
    reset = _TOKEN.set(t or "")
    try:
        yield
    finally:
        _TOKEN.reset(reset)


def repo_url(url):
    """(owner, repo) of a github.com https URL, or GitError."""
    m = _REPO_URL.match(str(url or "").strip())
    if not m:
        raise GitError("Use a GitHub https URL, like https://github.com/you/project.")
    return m.group(1), m.group(2)


def git_env():
    """The environment git runs with: no system or global config, no prompts,
    https only, and the token as a header for github.com alone."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
    }
    t = token()
    if t:
        basic = base64.b64encode(f"x-access-token:{t}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"})
    return env


def git_process(argv, cwd, env):
    """Run git. Hosting replaces this to run it inside the sandbox."""
    return subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=TIMEOUT)


def git(args, cwd, network=False, strip=True):
    """`git <args>` in `cwd`: (exit code, combined output, trimmed unless
    `strip` is False — porcelain output's leading space means something)."""
    argv = ["git", *SAFE_CONFIG, *args]
    try:
        done = git_process(argv, cwd, git_env())
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took longer than {TIMEOUT}s and was stopped.")
    except OSError as e:
        raise GitError(f"Couldn't start git: {e}")
    out = (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode("utf-8", "replace")
    t = token()
    if t:
        out = out.replace(t, "***")
    if len(out) > OUTPUT_MAX:
        out = out[:OUTPUT_MAX // 2] + "\n…\n" + out[-OUTPUT_MAX // 2:]
    return done.returncode, (out.strip() if strip else out)


# ── what went wrong, in plain words ──

_TOKEN_PAGE = "github.com/settings/personal-access-tokens"

# (what git or GitHub said, lowercased → what to tell the person). First match
# wins; the order runs from the most to the least specific.
_EXPLAIN = [
    (("gh006", "protected branch"),
     "{branch} is protected on GitHub, so it only takes changes through a pull request. Push to "
     "a new branch instead, then open a pull request."),
    (("non-fast-forward", "fetch first", "[rejected]", "updates were rejected"),
     "GitHub has commits this copy doesn't. Check origin and Pull first, then push again."),
    (("not possible to fast-forward", "diverging branches", "divergent branches"),
     "This copy and GitHub both have new commits, so Pull can't just catch up. Push your commits "
     "to a new branch and merge them on GitHub."),
    # Before the 403 below: a proxy refusing the tunnel says 403 too.
    (("could not resolve host", "connect tunnel failed", "failed to connect", "connection timed out",
      "operation timed out"),
     "Couldn't reach GitHub just now. Try again in a minute."),
    (("permission to", "403", "write access to repository not granted"),
     "Your GitHub token can read this repo but can't write to it. On GitHub ({page}), give the "
     "token access to this repo with Contents: Read and write (a classic token needs the repo "
     "scope), then try again."),
    (("authentication failed", "invalid username or token", "invalid username or password",
      "401", "bad credentials"),
     "GitHub didn't accept your token: it may have expired or been revoked. Make a new one "
     "({page}) and paste it into App Store → GitHub."),
    (("repository not found", "404"),
     "GitHub can't find this repo with your token. Check the URL, and if it's private, give the "
     "token access to it ({page})."),
    (("unable to access",),
     "Couldn't reach GitHub just now. Try again in a minute."),
]


def explain(out, fallback, branch=None):
    """A GitError for a failed clone, fetch, pull or push: a plain sentence
    for the cases people hit, with GitHub's own last line under it."""
    text = (out or "").strip()
    low = text.lower()
    last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
    for needles, message in _EXPLAIN:
        if any(n in low for n in needles):
            message = message.format(page=_TOKEN_PAGE, branch=branch or "That branch")
            return GitError(message + (f"\n(GitHub said: {last})" if last else ""))
    return GitError(text or fallback)


# ── who commits ──────────────────────────────────────────────

_identity = {}


def identity():
    """(name, email) for commits: the GitHub account the token belongs to,
    with its private noreply address, so a commit never shows an address the
    person didn't choose to publish. Asked once per process."""
    t = token()
    if not t:
        return "FreeClaw", "freeclaw@users.noreply.github.com"
    if t not in _identity:
        try:
            req = urllib.request.Request("https://api.github.com/user", headers={
                "Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json",
                "User-Agent": "FreeClaw"})
            with urllib.request.urlopen(req, timeout=10) as r:
                me = json.load(r)
            login = me.get("login") or "freeclaw"
            _identity[t] = (me.get("name") or login, f"{me.get('id')}+{login}@users.noreply.github.com")
        except (OSError, ValueError):
            return "FreeClaw", "freeclaw@users.noreply.github.com"
    return _identity[t]


# ── the operations ───────────────────────────────────────────
# `root` is the workspace and `repo` a folder inside it (already resolved by
# the caller); each returns a short text for the agent or the page.

def is_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))


def _need_repo(repo):
    if not is_repo(repo):
        raise GitError("That folder isn't a git repo. Clone one with git_clone first.")


def _need_token():
    if not token():
        raise GitError("Connect GitHub first: App Store → GitHub, with a token that can read "
                       "and write your repos.")


def clone(root, url, folder=None):
    owner, name = repo_url(url)
    _need_token()
    target = folder or name
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", target) or target in (".", ".."):
        raise GitError("Clone into a plain folder name, like my-project.")
    dest = os.path.join(root, target)
    if os.path.exists(dest):
        raise GitError(f"{target} already exists in projects.")
    code, out = git(["clone", "--no-recurse-submodules", f"https://github.com/{owner}/{name}.git", target],
                    root, network=True)
    if code != 0:
        raise explain(out, "The clone failed.")
    return f"Cloned {owner}/{name} into {target}."


def status(repo):
    _need_repo(repo)
    code, out = git(["status", "--short", "--branch"], repo)
    return out or "Clean."


def diff(repo, file=None, staged=False):
    """Changed lines only: a stat line per file, then the hunks with one line
    of context, cut at DIFF_MAX."""
    _need_repo(repo)
    args = ["diff", "--no-color", "--no-ext-diff", "--unified=1"] + (["--staged"] if staged else [])
    if file:
        args += ["--", file]
    _, stat = git(args[:4] + ["--stat"] + args[4:], repo)
    _, body = git(args, repo)
    out = (stat + "\n\n" + body).strip()
    if len(out) > DIFF_MAX:
        out = out[:DIFF_MAX] + f"\n… cut; ask for one file with file=."
    return out or "No changes."


def commit(repo, message, files=None):
    _need_repo(repo)
    message = str(message or "").strip()
    if not message:
        raise GitError("A commit needs a message.")
    if files:
        code, out = git(["add", "--", *[str(f) for f in files]], repo)
    else:
        code, out = git(["add", "-A"], repo)
    if code != 0:
        raise GitError(out)
    name, email = identity()
    code, out = git(["-c", f"user.name={name}", "-c", f"user.email={email}",
                     "commit", "--no-verify", "-m", message], repo)
    if code != 0:
        raise GitError(out or "Nothing to commit.")
    return out.splitlines()[0] if out else "Committed."


def current_branch(repo):
    """The checked-out branch, including one with no commits yet (rev-parse
    fails there and its error would read as a branch name)."""
    code, out = git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    return out.strip() if code == 0 else ""


def _origin(repo):
    """The GitHub repo `origin` points at, checked: code in the workspace can
    edit .git/config, and a push or pull only ever goes to GitHub."""
    _, url = git(["remote", "get-url", "origin"], repo)
    owner, name = repo_url(url)
    return f"https://github.com/{owner}/{name}.git"


def push(repo, branch=None):
    _need_repo(repo)
    _need_token()
    origin = _origin(repo)
    branch = branch or current_branch(repo)
    if not branch:
        raise GitError("Not on a branch (a detached checkout). Say which branch to push to.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", branch or ""):
        raise GitError("Bad branch name.")
    code, out = git(["push", "--no-verify", origin, f"HEAD:refs/heads/{branch}"], repo, network=True)
    if code != 0:
        raise explain(out, "The push failed.", branch)
    return f"Pushed to {branch}.\n{out}".strip()


def pull(repo):
    _need_repo(repo)
    _need_token()
    origin = _origin(repo)
    code, out = git(["pull", "--ff-only", "--no-recurse-submodules", origin, current_branch(repo)],
                    repo, network=True)
    if code != 0:
        raise explain(out, "The pull failed (it only fast-forwards; commit or stash local changes).")
    return out or "Up to date."


def branch(repo, name=None, switch=False):
    _need_repo(repo)
    if not name:
        _, out = git(["branch", "--list", "--no-color"], repo)
        return out or "No branches yet."
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", name):
        raise GitError("Bad branch name.")
    existing = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], repo)[0] == 0
    if switch:
        code, out = git(["switch", name] if existing else ["switch", "-c", name], repo)
    elif existing:
        return f"{name} already exists."
    else:
        code, out = git(["branch", name], repo)
    if code != 0:
        raise GitError(out)
    return f"On {name}." if switch else f"Created {name}."


# ── origin: what's there, and bringing it in ─────────────────

def _counts(repo, branch):
    """(ahead, behind) of HEAD against origin/<branch>, or (None, None) when
    there's no such remote branch yet."""
    code, out = git(["rev-list", "--left-right", "--count", f"HEAD...refs/remotes/origin/{branch}"], repo)
    if code != 0:
        return None, None
    try:
        ahead, behind = (int(x) for x in out.split())
        return ahead, behind
    except ValueError:
        return None, None


def origin(repo):
    """The repo's origin as last fetched: {url, branch, ahead, behind,
    incoming: [one-line commits on origin not here], outgoing: [the reverse]}.
    No network; fetch() first for a fresh answer."""
    _need_repo(repo)
    _, url = git(["remote", "get-url", "origin"], repo)
    branch = current_branch(repo)
    ahead, behind = _counts(repo, branch) if branch else (None, None)
    info = {"url": url if _REPO_URL.match(url or "") else "", "branch": branch,
            "ahead": ahead, "behind": behind, "incoming": [], "outgoing": []}
    if behind:
        _, out = git(["log", "--no-color", "--format=%h %s (%an, %ar)", "-n", "20",
                      f"HEAD..refs/remotes/origin/{branch}"], repo)
        info["incoming"] = out.splitlines()
    if ahead:
        _, out = git(["log", "--no-color", "--format=%h %s", "-n", "20",
                      f"refs/remotes/origin/{branch}..HEAD"], repo)
        info["outgoing"] = out.splitlines()
    return info


def fetch(repo):
    """Check origin: fetch from GitHub, then say what's new there."""
    _need_repo(repo)
    _need_token()
    url = _origin(repo)
    branch = current_branch(repo)
    code, out = git(["fetch", "--no-recurse-submodules", "--prune", url,
                     f"+refs/heads/*:refs/remotes/origin/*"], repo, network=True)
    if code != 0:
        raise explain(out, "Couldn't reach GitHub.")
    return origin_text(origin(repo))


def origin_text(info):
    """origin()'s answer as a few lines, for the agent."""
    if info["behind"] is None:
        return f"{info['branch']} isn't on GitHub yet; push it to create it."
    lines = [f"{info['branch']}: {info['behind']} new on GitHub, {info['ahead']} here not pushed."]
    if info["incoming"]:
        lines += ["Incoming (git_pull to bring them in):"] + ["  " + c for c in info["incoming"]]
    if info["outgoing"]:
        lines += ["Not pushed yet:"] + ["  " + c for c in info["outgoing"]]
    return "\n".join(lines)


def changes(repo):
    """Changed files as [{"path", "state"}]: state is one of modified, added,
    deleted, renamed, untracked or conflict."""
    _need_repo(repo)
    code, out = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], repo, strip=False)
    if code != 0:
        raise GitError(out)
    items, parts = [], out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x == "R":
            i += 1                                   # the old name follows a rename
        if x == "?":
            state = "untracked"
        elif "U" in (x, y) or (x, y) in (("A", "A"), ("D", "D")):
            state = "conflict"
        elif "D" in (x, y):
            state = "deleted"
        elif x == "A":
            state = "added"
        elif x == "R":
            state = "renamed"
        else:
            state = "modified"
        items.append({"path": path, "state": state})
    return items


def my_repos():
    """The person's GitHub repos, most recently pushed first, as
    [{"name": "owner/repo", "url", "private"}] (up to 100)."""
    _need_token()
    req = urllib.request.Request(
        "https://api.github.com/user/repos?per_page=100&sort=pushed&affiliation=owner,collaborator,organization_member",
        headers={"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
                 "User-Agent": "FreeClaw"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except (OSError, ValueError) as e:
        raise GitError(f"Couldn't list your GitHub repos: {e}")
    return [{"name": d.get("full_name"), "url": d.get("clone_url"), "private": bool(d.get("private"))}
            for d in data if isinstance(d, dict) and _REPO_URL.match(d.get("clone_url") or "")]
