"""Seed .env during a Windows install — the job install.sh does on Linux.

Called by the Inno Setup installer (windows/installer.iss) after the files are
in place. Kept in Python rather than in the installer's Pascal script for two
reasons: SECRET_KEY needs a real CSPRNG, which Pascal's Random() is not, and
merge-don't-clobber is fiddly enough that it deserves to be testable.

The merge rule is the important part. On an upgrade, .env is not a file the
installer created — by then it holds the user's providers, their MCP servers,
their install_id and whatever they typed into Settings. Every key here is
therefore written only if it is *absent*, and nothing is ever removed.

Usage:
    python write_env.py --env <path> [--password-file <path>] [--telemetry 0|1]
"""

import argparse
import contextlib
import os
import secrets
import sys


def read_keys(path):
    """Keys already present in .env. Values aren't needed — nothing is
    rewritten — and not reading them keeps provider keys out of this process."""
    keys = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.add(line.split("=", 1)[0].strip())
    except FileNotFoundError:
        pass
    return keys


def read_password_file(path):
    """Read the password the wizard collected, then delete the file.

    A file rather than a command-line argument on purpose: arguments are
    visible to anything that can list processes for the few milliseconds the
    call is alive, and this is the credential guarding the whole web UI.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip("\r\n")
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def seed(env_path, password=None, telemetry=None):
    """Append whichever of the three startup keys .env is missing.

    Returns the list of keys written, so the installer log says what happened.
    """
    existing = read_keys(env_path)
    additions = []

    if password is not None and "FC_PASSWORD" not in existing:
        additions.append(("FC_PASSWORD", password))
    if "SECRET_KEY" not in existing:
        # Same role as the Linux installer's secret_key: without it Flask
        # generates a throwaway per boot and every restart logs everyone out.
        additions.append(("SECRET_KEY", secrets.token_hex(32)))
    if telemetry is not None and "FC_TELEMETRY" not in existing:
        additions.append(("FC_TELEMETRY", "1" if telemetry else "0"))

    if not additions:
        return []

    # Append rather than rewrite, and make sure we start on a fresh line: a
    # hand-edited .env may well not end in a newline, and joining two keys
    # onto one line would break both.
    needs_newline = False
    if os.path.exists(env_path) and os.path.getsize(env_path):
        with open(env_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")

    with open(env_path, "a", encoding="utf-8", newline="\n") as f:
        if needs_newline:
            f.write("\n")
        for key, value in additions:
            f.write(f"{key}={value}\n")

    return [key for key, _ in additions]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="path to .env")
    parser.add_argument("--password-file",
                        help="file holding the login password; deleted after reading")
    parser.add_argument("--telemetry", choices=("0", "1"))
    args = parser.parse_args(argv)

    password = None
    if args.password_file and os.path.exists(args.password_file):
        password = read_password_file(args.password_file) or None

    telemetry = None if args.telemetry is None else args.telemetry == "1"

    written = seed(args.env, password=password, telemetry=telemetry)
    # Never echo the values — this output goes to the installer's log.
    print("wrote: " + (", ".join(written) if written else "nothing (all keys present)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
