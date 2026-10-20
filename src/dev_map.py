"""A map of a project for the agent: what's there and where things are
defined, without reading the code.

Three answers, each a few KB at most however big the project:

* overview(): languages, the main folders with their file counts, the files
  that say what a project is (README, package.json, pyproject.toml…) and
  likely entry points;
* outline(): the classes, functions and headings of a file or folder, with
  line numbers and signatures, and no bodies;
* find(): where a name is defined, as path:line and its signature.

Python is parsed (ast); the other common languages are read line by line
with patterns, which gets the definitions people look for and occasionally a
false one. Results are kept per file until its size or modified time
changes, so asking again is cheap.

Files are listed the way git sees them inside a repo (tracked plus untracked,
minus what .gitignore excludes), so build output and dependencies don't
crowd the answers; outside a repo, everything but the usual dependency and
build folders.
"""
import ast
import fnmatch
import os
import re

MAX_FILES = 5000             # files considered in one call
MAX_FILE_BYTES = 1_000_000   # bigger files aren't outlined
OUTLINE_MAX_LINES = 300
FIND_MAX = 40
SIGNATURE_CHARS = 110

LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin", ".swift": "Swift", ".cs": "C#", ".c": "C", ".h": "C/C++",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".rb": "Ruby", ".php": "PHP", ".sh": "Shell",
    ".bash": "Shell", ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".vue": "Vue", ".svelte": "Svelte",
    ".md": "Markdown", ".sql": "SQL", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML",
}

# What says what a project is, in the order it's worth reading.
KEY_FILES = ("README.md", "README.rst", "README.txt", "README", "package.json", "pyproject.toml",
             "setup.py", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
             "Gemfile", "composer.json", "Makefile", "Dockerfile", "docker-compose.yml")
ENTRY_POINTS = ("main.py", "app.py", "manage.py", "server.py", "__main__.py", "index.js", "index.ts",
                "main.js", "main.ts", "server.js", "app.js", "main.go", "main.rs", "Main.java",
                "index.html")

_cache = {}   # path -> ((size, mtime), symbols)


# ── which files ──────────────────────────────────────────────

def _repo_root(path, root):
    """The git repo `path` is in, within the workspace `root`, or None."""
    p = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return p
        if p == root or len(p) <= len(root):
            return None
        p = os.path.dirname(p)


def files_under(root, top, skip_dirs, git=None):
    """Files under `top`, git-aware when it's in a repo and `git` (a
    `dev_git.git`-shaped callable) is given. At most MAX_FILES."""
    repo = _repo_root(top, root) if git else None
    out = []
    if repo:
        try:
            code, listing = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard",
                                 "--", os.path.relpath(top, repo) or "."], repo, strip=False)
        except Exception:                                    # noqa: BLE001 — fall back to walking
            code = 1
        if code == 0:
            for rel in listing.split("\0"):
                # The usual dependency and build folders, whatever this
                # repo's .gitignore says: an untracked node_modules is
                # still not the project.
                if rel and not any(part in skip_dirs for part in rel.split("/")[:-1]):
                    full = os.path.join(repo, rel)
                    if os.path.isfile(full) and not os.path.islink(full):
                        out.append(full)
                        if len(out) >= MAX_FILES:
                            break
            return sorted(out)
    for folder, dirs, names in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not os.path.islink(os.path.join(folder, d)))
        for name in sorted(names):
            full = os.path.join(folder, name)
            if not os.path.islink(full):
                out.append(full)
                if len(out) >= MAX_FILES:
                    return out
    return out


# ── symbols ──────────────────────────────────────────────────
# A symbol is (line, depth, kind, name, signature).

def _sig(line):
    line = " ".join(line.strip().split())
    line = line.rstrip("{:").rstrip()
    return line if len(line) <= SIGNATURE_CHARS else line[:SIGNATURE_CHARS] + "…"


def _python(text, lines):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = []

    def walk(nodes, depth, prefix):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                sig = _sig(lines[node.lineno - 1]) if node.lineno - 1 < len(lines) else node.name
                out.append((node.lineno, depth, kind, prefix + node.name, sig))
                if depth < 2:
                    walk(node.body, depth + 1, prefix + node.name + ".")
    walk(tree.body, 0, "")
    return out


_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "with", "else", "do"}
_PATTERNS = {
    "js": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)"), "function"),
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
        (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"), "type"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
                    r"(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"), "function"),
        (re.compile(r"^\s{2,}(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"), "method"),
    ],
    "go": [
        (re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"), "func"),
        (re.compile(r"^type\s+([A-Za-z_]\w*)"), "type"),
    ],
    "rs": [
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"), "fn"),
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|mod)\s+([A-Za-z_]\w*)"), "type"),
        (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_][\w:]*)"), "impl"),
    ],
    "rb": [
        (re.compile(r"^\s*(?:class|module)\s+([A-Z][\w:]*)"), "class"),
        (re.compile(r"^\s*def\s+((?:self\.)?[\w?!=]+)"), "def"),
    ],
    "php": [
        (re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)"), "function"),
    ],
    "jvm": [   # Java, Kotlin, C#, Swift, Dart
        (re.compile(r"^\s*(?:[\w@]+\s+)*(?:class|interface|enum|struct|record|object|protocol|extension)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|override|open|abstract|async|"
                    r"suspend|virtual|sealed|inline)\s+)*(?:fun|func)\s+([A-Za-z_]\w*)"), "func"),
        (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|override|abstract|async|virtual|"
                    r"synchronized)\s+)+[\w<>\[\],.? ]+\s+([A-Za-z_]\w*)\s*\([^;]*$"), "method"),
    ],
    "c": [
        (re.compile(r"^(?:class|struct|union|enum)\s+([A-Za-z_]\w*)\s*(?:[:{]|$)"), "type"),
        (re.compile(r"^[A-Za-z_][\w\s\*&:<>,]*?\b([A-Za-z_][\w:~]*)\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$"), "function"),
    ],
    "sh": [
        (re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"), "function"),
    ],
    "sql": [
        (re.compile(r"^\s*create\s+(?:or\s+replace\s+)?(?:table|view|function|procedure|index)\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)", re.I), "create"),
    ],
}
_FAMILY = {".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".vue": "js",
           ".svelte": "js", ".go": "go", ".rs": "rs", ".rb": "rb", ".php": "php", ".java": "jvm",
           ".kt": "jvm", ".kts": "jvm", ".cs": "jvm", ".swift": "jvm", ".dart": "jvm", ".c": "c",
           ".h": "c", ".cpp": "c", ".cc": "c", ".hpp": "c", ".sh": "sh", ".bash": "sh", ".sql": "sql"}
_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")


def _lines_symbols(lines, family):
    out = []
    for n, line in enumerate(lines, 1):
        for pattern, kind in _PATTERNS[family]:
            m = pattern.match(line)
            if m and m.group(1) not in _JS_KEYWORDS:
                indent = len(line) - len(line.lstrip())
                out.append((n, 1 if indent >= 2 and kind == "method" else 0, kind, m.group(1), _sig(line)))
                break
    return out


def _markdown(lines):
    out, fence = [], False
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            m = _HEADING.match(line)
            if m:
                out.append((n, len(m.group(1)) - 1, "h" + str(len(m.group(1))), m.group(2), line.strip()))
    return out


def symbols(path):
    """The file's symbols, cached until it changes."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_size, st.st_mtime_ns)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    ext = os.path.splitext(path)[1].lower()
    result = []
    if st.st_size <= MAX_FILE_BYTES and (ext == ".py" or ext in _FAMILY or ext == ".md"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        if "\x00" not in text[:8192]:
            lines = text.splitlines()
            if ext == ".py":
                result = _python(text, lines)
                if result is None:                           # doesn't parse: patterns, then
                    result = [(n, 0 if not l[:1].isspace() else 1, "def" if "def " in l else "class",
                               re.search(r"(?:def|class)\s+(\w+)", l).group(1), _sig(l))
                              for n, l in enumerate(lines, 1)
                              if re.match(r"^\s*(?:async\s+)?(?:def|class)\s+\w+", l)]
            elif ext == ".md":
                result = _markdown(lines)
            else:
                result = _lines_symbols(lines, _FAMILY[ext])
    _cache[path] = (key, result)
    if len(_cache) > 20000:
        _cache.clear()
    return result


# ── the three answers ────────────────────────────────────────

def outline(root, top, rel_of, files):
    """The structure of `top` (a file, or every file in a folder)."""
    if os.path.isfile(top):
        syms = symbols(top)
        if not syms:
            return f"{rel_of(root, top)}: nothing to outline (no classes, functions or headings found)."
        lines = [f"{rel_of(root, top)}"] + [f"{n:>6}  {'  ' * depth}{sig}" for n, depth, _, _, sig in syms]
    else:
        lines, shown = [], 0
        for path in files:
            syms = symbols(path)
            if not syms:
                continue
            lines.append(rel_of(root, path))
            # Folder view: top-level definitions only, to keep it a map.
            lines += [f"{n:>6}  {sig}" for n, depth, _, _, sig in syms if depth == 0]
            shown += 1
        if not lines:
            return "Nothing to outline here."
    if len(lines) > OUTLINE_MAX_LINES:
        more = len(lines) - OUTLINE_MAX_LINES
        lines = lines[:OUTLINE_MAX_LINES] + [f"[… {more} more lines; outline a subfolder or one file]"]
    return "\n".join(lines)


def find(root, name, rel_of, files, partial=False):
    """Where `name` is defined: path:line  signature."""
    name = str(name or "").strip()
    if not name:
        return "Give a name to find."
    want = name.lower()
    hits, more = [], 0
    for path in files:
        for n, _, _, sym, sig in symbols(path):
            last = sym.rsplit(".", 1)[-1].lower()
            full = sym.lower()
            if (want in last) if partial else (last == want or full == want):
                if len(hits) >= FIND_MAX:
                    more += 1
                    continue
                hits.append(f"{rel_of(root, path)}:{n}  {sig}")
    if not hits:
        return (f"No definition of {name} found. Try partial: true, or search for it as text."
                if not partial else f"No definition matching {name}.")
    if more:
        hits.append(f"[… {more} more; be more specific]")
    return "\n".join(hits)


def overview(root, top, rel_of, files):
    """A one-screen summary of `top`."""
    langs, dirs, total = {}, {}, 0
    for path in files:
        total += 1
        lang = LANGUAGES.get(os.path.splitext(path)[1].lower())
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
        rel = os.path.relpath(path, top)
        first = rel.split(os.sep, 1)[0] if os.sep in rel else "."
        dirs[first] = dirs.get(first, 0) + 1
    out = [f"{rel_of(root, top)}: {total} files" + (" (capped)" if total >= MAX_FILES else "")]
    if langs:
        out.append("Languages: " + ", ".join(f"{k} {v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])[:8]))
    if dirs:
        parts = [f"{d}/ {c}" for d, c in sorted(dirs.items(), key=lambda kv: -kv[1]) if d != "."][:12]
        if parts:
            out.append("Folders: " + ", ".join(parts) + (f", top level {dirs['.']}" if "." in dirs else ""))
    names = {os.path.basename(p): p for p in files if os.path.dirname(p) == top}
    for key in KEY_FILES:
        path = names.get(key)
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(4000)
        except OSError:
            continue
        if key.startswith("README"):
            body = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith(("![", "<", "[!["))]
            out.append(f"{key}: " + " / ".join(body[:6])[:600])
        elif key == "package.json":
            import json
            try:
                pkg = json.loads(open(path, encoding="utf-8").read())
                bits = [pkg.get("name") or "", pkg.get("description") or ""]
                if pkg.get("scripts"):
                    bits.append("scripts: " + ", ".join(list(pkg["scripts"])[:10]))
                if pkg.get("dependencies"):
                    bits.append("deps: " + ", ".join(list(pkg["dependencies"])[:15]))
                out.append("package.json: " + " · ".join(b for b in bits if b))
            except (OSError, ValueError):
                out.append("package.json (unreadable)")
        else:
            first = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")][:8]
            out.append(f"{key}: " + " · ".join(first)[:300])
    entries = [rel_of(root, p) for p in files if os.path.basename(p) in ENTRY_POINTS][:8]
    if entries:
        out.append("Likely entry points: " + ", ".join(entries))
    out.append("Next: outline a folder or file, find a name, search text, then read only the lines you need.")
    return "\n".join(out)


def match_glob(path, rel, glob):
    return not glob or fnmatch.fnmatch(os.path.basename(path), glob) or fnmatch.fnmatch(rel, glob)
