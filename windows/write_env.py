"""Seed .env during a Windows install — the job install.sh does on Linux.

Called by install.ps1 once the files are in place. Kept in Python rather than
written inline in PowerShell for two reasons: SECRET_KEY and the generated
password need a real CSPRNG, and Windows PowerShell 5.1 runs on a .NET where
the modern crypto helpers are missing and fail by returning zeroes rather than
by throwing. Merge-don't-clobber is also fiddly enough to deserve being
testable on its own.

The merge rule is the important part. On an upgrade, .env is not a file the
installer created — by then it holds the user's providers, their MCP servers,
their install_id and whatever they typed into Settings. Every key here is
therefore written only if it is *absent*, and nothing is ever removed.

Usage:
    python write_env.py --env <path> [--password-file <path>] [--telemetry 0|1]
    python write_env.py --env <path> --generate-password [--telemetry 0|1]
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


# Ambiguous glyphs removed: this gets read off a terminal and typed into a
# browser, and l/I/1 and O/0 are where that goes wrong. 16 characters of the
# remaining 57 is ~93 bits, far past anything that matters for a LAN login.
_PASSWORD_ALPHABET = ("abcdefghijkmnopqrstuvwxyz"
                      "ABCDEFGHJKLMNPQRSTUVWXYZ"
                      "23456789")


def generate_password(length=16):
    """A strong login password, from the same CSPRNG as SECRET_KEY.

    Here rather than in install.ps1 because PowerShell got this wrong in a way
    that was silent: RandomNumberGenerator.Fill() does not exist on the .NET
    Framework behind Windows PowerShell 5.1, so the buffer stayed zeroed and
    every install got the same password. Python's secrets module is available
    wherever this script runs at all.
    """
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def seed(env_path, password=None, telemetry=None):
    """Append whichever of the three startup keys .env is missing.

    Returns the list of keys written, so the caller can report what happened.
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
    parser.add_argument("--generate-password", action="store_true",
                        help="invent a password if .env has none, and print it")
    args = parser.parse_args(argv)

    password = None
    if args.password_file and os.path.exists(args.password_file):
        password = read_password_file(args.password_file) or None
    elif args.generate_password:
        password = generate_password()

    telemetry = None if args.telemetry is None else args.telemetry == "1"

    written = seed(args.env, password=password, telemetry=telemetry)

    # A generated password is the one value worth echoing: nobody chose it, so
    # if it is not shown here there is no way back into the web UI. Everything
    # else stays unprinted — a password given explicitly is already known.
    if args.generate_password and "FC_PASSWORD" in written:
        print("FC_PASSWORD=" + password)
    print("wrote: " + (", ".join(written) if written else "nothing (all keys present)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
