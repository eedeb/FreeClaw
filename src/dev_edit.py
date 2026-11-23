"""Editing helpers for the Developer app: a forgiving exact-text match for
`edit`, and the `patch` tool's format.

The match. `edit` replaces an exact piece of text, and exact is what goes
wrong: a model copies a line with a curly quote where the file has a straight
one, an en dash for a hyphen, a non-breaking space, or trailing spaces the
file doesn't have. Each costs a failed call and a re-read. So when the exact
text isn't there, it's looked for again, first ignoring trailing whitespace on
each line, then also those character variants. It's only taken when it's
found exactly once: a looser match that's ambiguous is no match. What's
replaced is always the file's own text at that place, and a file with CRLF
line endings keeps them.

The patch. One call that adds, changes, renames and deletes several files, in
the format OpenAI's models are trained to write (apply_patch):

    *** Begin Patch
    *** Add File: projects/app/hello.txt
    +Hello world
    *** Update File: projects/app/main.py
    *** Move to: projects/app/app.py
    @@ def greet():
    -    print("Hi")
    +    print("Hello, world!")
    *** Delete File: projects/app/old.txt
    *** End Patch

Each changed section of an Update ("hunk") is its context lines (" "),
removed lines ("-") and added lines ("+"); an "@@ <line>" header names a line
the hunk comes after, for when the context alone isn't unique. Hunks apply in
order, each searched from where the last one ended, with the same forgiving
line match as `edit`. All of it is worked out before anything is written: a
patch that fails anywhere changes nothing.
"""
import re

# Characters models swap for their plain look-alikes.
_VARIANTS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", "　": " ", "…": "...",
})


class EditError(Exception):
    """A mistake the model can fix, said so it can."""


def _plain(line):
    return line.translate(_VARIANTS).rstrip()


# The tiers, strictest first: exact, then trailing whitespace ignored, then
# look-alike characters too.
_TIERS = (lambda s: s, lambda s: s.rstrip(), _plain)


def find(text, old):
    """(start, end, tier) of the one place `old` is in `text`, comparing line
    by line from tier 0 (exact) up; or (None, count, tier) when the first tier
    with any match has `count` of them (count 0: nowhere). Tier 0 is a plain
    substring search; the looser tiers match whole lines."""
    count = text.count(old)
    if count == 1:
        start = text.index(old)
        return start, start + len(old), 0
    if count > 1:
        return None, count, 0
    lines = text.splitlines(keepends=True)
    want = old.splitlines()
    if not want:
        return None, 0, 0
    # A trailing empty line in old (it ended with "\n") is the line break, not a line.
    for tier, norm in enumerate(_TIERS[1:], start=1):
        target = [norm(w) for w in want]
        have = [norm(line.rstrip("\r\n")) for line in lines]
        hits = [i for i in range(len(have) - len(target) + 1) if have[i:i + len(target)] == target]
        if len(hits) == 1:
            i = hits[0]
            start = sum(len(line) for line in lines[:i])
            span = lines[i:i + len(target)]
            end = start + sum(len(line) for line in span)
            # old didn't end in a line break: neither does what it replaces.
            if not old.endswith(("\n", "\r")) and span:
                last = span[-1]
                end -= len(last) - len(last.rstrip("\r\n"))
            return start, end, tier
        if len(hits) > 1:
            return None, len(hits), tier
    return None, 0, 0


def line_ending(text):
    """The file's line break: "\r\n" when that's what it mostly uses."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf * 2 >= text.count("\n") else "\n"


def with_endings(new, text):
    """`new` with the line breaks `text` uses, so an edit doesn't mix them."""
    if line_ending(text) == "\r\n":
        return re.sub(r"(?<!\r)\n", "\r\n", new)
    return new


# ── the patch format ─────────────────────────────────────────

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: "
EOF_MARK = "*** End of File"
EXAMPLE = ("*** Begin Patch\n*** Add File: projects/app/new.py\n+first line\n"
           "*** Update File: projects/app/main.py\n*** Move to: projects/app/app.py\n"
           "@@ def greet():\n-    print(\"Hi\")\n+    print(\"Hello\")\n"
           "*** Delete File: projects/app/old.py\n*** End Patch\n"
           "(' ' context, '-' removed, '+' added; '@@ <line>' names a line the change comes after.)")


def parse(patch_text):
    """[{"op": "add"|"delete"|"update", "path", "move_to", "content", "hunks"}]
    from a patch, or an EditError saying what's wrong and where."""
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise EditError("patch is empty.")
    lines = patch_text.replace("\r\n", "\n").split("\n")
    # Fenced in ``` by habit: the fence isn't part of it.
    while lines and (not lines[0].strip() or lines[0].strip().startswith("```")):
        lines.pop(0)
    while lines and (not lines[-1].strip() or lines[-1].strip().startswith("```")):
        lines.pop()
    if not lines or lines[0].strip() != BEGIN:
        raise EditError(f"A patch starts with the line '{BEGIN}'. The format:\n{EXAMPLE}")
    if lines[-1].strip() != END:
        raise EditError(f"A patch ends with the line '{END}'.")
    body, ops, i = lines[1:-1], [], 0
    while i < len(body):
        line = body[i]
        if line.startswith(ADD):
            path, content, i = line[len(ADD):].strip(), [], i + 1
            while i < len(body) and not body[i].startswith("*** "):
                if not body[i].startswith("+"):
                    raise EditError(f"Add File {path}: every line of a new file starts with '+' "
                                    f"(line {i + 2} of the patch doesn't).")
                content.append(body[i][1:])
                i += 1
            ops.append({"op": "add", "path": path, "content": "\n".join(content) + ("\n" if content else "")})
        elif line.startswith(DELETE):
            ops.append({"op": "delete", "path": line[len(DELETE):].strip()})
            i += 1
        elif line.startswith(UPDATE):
            path, i = line[len(UPDATE):].strip(), i + 1
            move_to = None
            if i < len(body) and body[i].startswith(MOVE):
                move_to, i = body[i][len(MOVE):].strip(), i + 1
            hunks, hunk = [], None
            while i < len(body) and not (body[i].startswith("*** ") and body[i].strip() != EOF_MARK):
                row = body[i]
                if row.strip() == EOF_MARK:
                    if hunk is not None:
                        hunk["eof"] = True
                elif row.startswith("@@"):
                    hunk = {"header": row[2:].strip(), "old": [], "new": [], "eof": False}
                    hunks.append(hunk)
                else:
                    if hunk is None:
                        hunk = {"header": "", "old": [], "new": [], "eof": False}
                        hunks.append(hunk)
                    mark, rest = (row[:1], row[1:]) if row else (" ", "")
                    if mark == " ":
                        hunk["old"].append(rest)
                        hunk["new"].append(rest)
                    elif mark == "-":
                        hunk["old"].append(rest)
                    elif mark == "+":
                        hunk["new"].append(rest)
                    else:
                        raise EditError(f"Update File {path}: line {i + 2} of the patch starts with "
                                        f"{mark!r}; each line of a change starts with ' ', '-' or '+'.")
                i += 1
            hunks = [h for h in hunks if h["old"] or h["new"]]
            if not hunks and not move_to:
                raise EditError(f"Update File {path}: no changes in it.")
            ops.append({"op": "update", "path": path, "move_to": move_to, "hunks": hunks})
        elif not line.strip():
            i += 1
        else:
            raise EditError(f"Line {i + 2} of the patch isn't a file header ({ADD.strip()}, "
                            f"{UPDATE.strip()} or {DELETE.strip()}): {line[:80]!r}. The format:\n{EXAMPLE}")
    if not ops:
        raise EditError("The patch has no files in it.")
    for op in ops:
        if not op["path"]:
            raise EditError(f"A {op['op']} with no path.")
    return ops


def _seek(lines, want, start):
    """Index of `want` (a list of lines) in `lines` at or after `start`, by
    the loosest tier needed, first match; None if it isn't there."""
    if not want:
        return start
    for norm in _TIERS:
        target = [norm(w) for w in want]
        have = [norm(x) for x in lines]
        for i in range(start, len(have) - len(target) + 1):
            if have[i:i + len(target)] == target:
                return i
    return None


def apply_update(text, hunks, path=""):
    """`text` with an Update's hunks applied in order, keeping its line breaks."""
    eol = line_ending(text)
    ends_with_break = text.endswith(("\n", "\r"))
    lines = text.replace("\r\n", "\n").split("\n")
    if ends_with_break:
        lines.pop()
    cursor = 0
    for n, h in enumerate(hunks, 1):
        if h["header"]:
            at = _seek(lines, [h["header"]], cursor)
            if at is None:
                raise EditError(f"{path}: change {n}'s @@ line {h['header']!r} isn't in the file "
                                f"(after the changes before it).")
            cursor = at + 1 if h["header"] not in h["old"][:1] else at
        if not h["old"]:
            at = len(lines) if h["eof"] or not h["header"] else cursor
        else:
            at = _seek(lines, h["old"], cursor)
            if h["eof"] and at is not None:
                end = _seek(lines, h["old"], max(cursor, len(lines) - len(h["old"])))
                at = end if end is not None else at
            if at is None:
                # Said with what was looked for, so it can be read again and fixed.
                shown = "\n".join(h["old"][:6])
                raise EditError(f"{path}: change {n} doesn't match the file. These lines weren't found"
                                f"{' after the change before it' if n > 1 else ''}:\n{shown}\n"
                                "Read that part of the file again and redo this change.")
        lines[at:at + len(h["old"])] = h["new"]
        cursor = at + len(h["new"])
    out = "\n".join(lines) + ("\n" if ends_with_break or not lines else "")
    return out.replace("\n", "\r\n") if eol == "\r\n" else out
