"""Undo what the agent's turn did to the person's files.

Before a file tool changes a file (create_file, edit_file, delete_file, and
the Developer app's write, edit and patch), the file as it was is kept, once
per turn: its bytes, or the fact that it didn't exist. Undoing a turn puts
every file it touched back as it was before it, and so does every later turn
first, newest first: they were built on it, and undoing one under them would
leave files from two different moments.

What code the agent ran changed isn't kept (a run can touch anything): undo
says so. Neither are its memory and schedule (context.md, ping.md): those are
the agent's own, not the person's work.

    <user folder>/.undo/<turn id>/record.json   {id, at, label, files: {rel: blob|null}}
    <user folder>/.undo/<turn id>/<blob>        a file's bytes before the turn

Beside conversation.json, outside files/, so the agent's own tools can't
reach it. The newest KEEP turns, for at most DAYS days; a file over FILE_MAX,
or past TURN_MAX for the turn, isn't kept, and undo says it couldn't put that
one back.
"""
import json
import os
import re
import shutil
import threading
import time
import uuid

FOLDER = ".undo"
KEEP = 10
DAYS = 7
FILE_MAX = 5 * 1024 * 1024
TURN_MAX = 20 * 1024 * 1024        # all of one turn's kept files together; past it, not kept
SKIP = {"context.md", "ping.md"}
TOO_BIG = "too-big"
_lock = threading.Lock()


def _root(user_dir):
    return os.path.join(user_dir, FOLDER)


def _rel(files_dir, path):
    """`path` relative to the person's files, or None if it isn't in them."""
    files_dir = os.path.realpath(files_dir)
    full = os.path.realpath(path)
    if full != files_dir and not full.startswith(files_dir + os.sep):
        return None
    rel = os.path.relpath(full, files_dir).replace(os.sep, "/")
    return None if rel in (".", "") or rel in SKIP else rel


def _load(folder):
    try:
        with open(os.path.join(folder, "record.json"), encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) and isinstance(rec.get("files"), dict) else None
    except (OSError, ValueError):
        return None


def _save(folder, rec):
    tmp = os.path.join(folder, "record.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, os.path.join(folder, "record.json"))


def records(user_dir):
    """The turns that can be undone, newest first: [{id, at, label, files}]."""
    root = _root(user_dir)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for name in names:
        rec = _load(os.path.join(root, name))
        if rec and rec.get("files"):
            out.append(rec)
    return sorted(out, key=lambda r: r.get("at", 0), reverse=True)


def _prune(user_dir, now, keep=KEEP):
    root = _root(user_dir)
    try:
        names = os.listdir(root)
    except OSError:
        return
    recs = []
    for name in names:
        rec = _load(os.path.join(root, name))
        if rec is None or now - rec.get("at", 0) > DAYS * 86400:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        else:
            recs.append(rec)
    for rec in sorted(recs, key=lambda r: r.get("at", 0), reverse=True)[keep:]:
        shutil.rmtree(os.path.join(root, rec["id"]), ignore_errors=True)


def new_turn_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def capture(user_dir, files_dir, turn_id, label, paths):
    """Keep each of `paths` as it is now, unless this turn already kept it."""
    if not user_dir or not files_dir or not turn_id or not re.fullmatch(r"[0-9A-Za-z-]{1,64}", turn_id):
        return
    with _lock:
        folder = os.path.join(_root(user_dir), turn_id)
        rec = _load(folder)
        fresh = rec is None
        if fresh:
            now = time.time()
            _prune(user_dir, now, KEEP - 1)        # room for this one
            rec = {"id": turn_id, "at": now, "label": " ".join(str(label or "").split())[:120], "files": {}}
        changed = False
        for path in paths:
            rel = _rel(files_dir, path)
            if rel is None or rel in rec["files"]:
                continue
            if os.path.isfile(path) and not os.path.islink(path):
                size = os.path.getsize(path)
                if size > FILE_MAX or rec.get("bytes", 0) + size > TURN_MAX:
                    rec["files"][rel] = TOO_BIG
                else:
                    os.makedirs(folder, exist_ok=True)
                    blob = f"{len(rec['files'])}.bin"
                    shutil.copyfile(path, os.path.join(folder, blob))
                    rec["files"][rel] = blob
                    rec["bytes"] = rec.get("bytes", 0) + size
            elif not os.path.exists(path):
                rec["files"][rel] = None            # made by the turn: undo deletes it
            else:
                continue                             # a folder or a link: not a file to keep
            changed = True
        if changed:
            os.makedirs(folder, exist_ok=True)
            _save(folder, rec)


def undo(user_dir, files_dir, turn_id):
    """Put the files back as they were before turn `turn_id`, undoing every
    later turn first. (restored, deleted, not_restored) relative paths; a
    ValueError if there's no such turn to undo."""
    with _lock:
        recs = records(user_dir)
        ids = [r["id"] for r in recs]
        if turn_id not in ids:
            raise ValueError("That turn's file changes can't be undone (already undone, or too old).")
        final = {}                     # rel -> outcome; the oldest record's is the last word
        files_dir = os.path.realpath(files_dir)
        for rec in recs[:ids.index(turn_id) + 1]:              # newest first, down to it
            folder = os.path.join(_root(user_dir), rec["id"])
            for rel, blob in rec["files"].items():
                path = os.path.join(files_dir, *rel.split("/"))
                if _rel(files_dir, path) != rel or blob == TOO_BIG:
                    final[rel] = "failed"
                    continue
                try:
                    if blob is None:
                        if os.path.isfile(path):
                            os.remove(path)
                        final[rel] = "deleted"
                    else:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        tmp = path + ".undo-tmp"
                        shutil.copyfile(os.path.join(folder, blob), tmp)
                        os.replace(tmp, path)
                        final[rel] = "restored"
                except OSError:
                    final[rel] = "failed"
            shutil.rmtree(folder, ignore_errors=True)
        return tuple(sorted(r for r, s in final.items() if s == kind)
                     for kind in ("restored", "deleted", "failed"))


def note(restored, deleted, failed, label):
    """What the agent is told after the person undid file changes."""
    parts = []
    if restored:
        parts.append("put back as they were: " + ", ".join(restored[:20]))
    if deleted:
        parts.append("removed (the turn had made them): " + ", ".join(deleted[:20]))
    if failed:
        parts.append("couldn't be put back: " + ", ".join(failed[:20]))
    return ("[Not typed by the user: they undid the file changes from your turn on "
            f"\"{label}\" and every turn after it. " + "; ".join(parts) + ". Files changed by code "
            "you ran weren't undone. Treat those files as they are now.]")
