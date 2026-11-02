#!/usr/bin/env python3
"""Set a few keys in the board's settings.toml without touching the rest.

    settings_toml.py KEYS_FILE BOARD_DIR

KEYS_FILE (tools/board_settings.toml) holds KEY = value lines.  For each, an
existing "KEY = ..." line in BOARD_DIR/settings.toml is replaced in place, or
the line is appended; every other line (Wi-Fi SSID and password, comments)
is kept byte for byte and never printed.  If the file changes, the old one
is backed up first to ~/pico-settings/ (dir 700, file 600, like
board_guard's settings.json backup).  Prints one line per key."""
import os
import sys
import time

SETTINGS_TOML = "settings.toml"
BACKUP_DIR = os.path.join("~", "pico-settings")
BACKUP_DIR_MODE = 0o700
BACKUP_FILE_MODE = 0o600
STAMP_FORMAT = "%Y%m%dT%H%M%S"
MAX_BACKUP_TRIES = 100


def key_of(line):
    """The key of a "KEY = value" line, else None (comments, tables, blank)."""
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("[") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip()


def wanted(text):
    """[(key, line)] from the repo's keys file, in order."""
    out = []
    for line in text.splitlines():
        k = key_of(line)
        if k:
            out.append((k, line.strip()))
    return out


def merge(board_text, keys):
    """(new_text, [(key, "set" | "added" | "unchanged")])."""
    lines = board_text.splitlines(True)
    report = []
    for k, line in keys:
        hit = [i for i, ln in enumerate(lines) if key_of(ln) == k]
        if not hit:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(line + "\n")
            report.append((k, "added"))
            continue
        i = hit[0]
        nl = "\r\n" if lines[i].endswith("\r\n") else "\n"
        if lines[i].strip() == line:
            report.append((k, "unchanged"))
        else:
            lines[i] = line + nl
            report.append((k, "set"))
        for j in reversed(hit[1:]):           # a duplicate key: keep one line only
            del lines[j]
    return "".join(lines), report


def backup(path, data):
    folder = os.path.expanduser(BACKUP_DIR)
    os.makedirs(folder, mode=BACKUP_DIR_MODE, exist_ok=True)
    os.chmod(folder, BACKUP_DIR_MODE)
    base = "settings.toml-%s" % time.strftime(STAMP_FORMAT)
    for n in range(MAX_BACKUP_TRIES):
        out = os.path.join(folder, base + ("-%d" % n if n else "") + ".toml")
        try:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, BACKUP_FILE_MODE)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(out, BACKUP_FILE_MODE)
        return out
    raise OSError("no free backup name for " + base)


def main(keys_file, board_dir):
    with open(keys_file) as f:
        keys = wanted(f.read())
    path = os.path.join(board_dir, SETTINGS_TOML)
    old = b""
    if os.path.isfile(path):
        with open(path, "rb") as f:
            old = f.read()
    new, report = merge(old.decode("utf-8"), keys)
    if new.encode("utf-8") != old:
        if old:
            print("settings.toml backed up to %s" % backup(path, old))
        with open(path, "wb") as f:
            f.write(new.encode("utf-8"))
    lines = dict(keys)
    for k, what in report:
        print("settings.toml %s: %s" % (what, lines[k]))   # repo values only, never the board's
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
