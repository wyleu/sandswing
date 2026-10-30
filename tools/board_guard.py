#!/usr/bin/env python3
# board_guard.py -- refuse to deploy onto the wrong Pico, and back up its
# settings.json first.  Called by tools/deploy.sh before anything is copied:
#
#   python3 tools/board_guard.py --project sandswing --dest /media/$USER/CIRCUITPY
#                                [--first-install] [--force]
#
# The same file lives in sandswing and sandtest (each repo is self-contained).
# Signals, in order:
#   1. build_info.py PROJECT = "..." on the board (written by deploy.sh);
#      older deploys had no PROJECT line, so then the project's own files
#      on the board are used instead.
#   2. the shape of settings.json on the board (keys only; values are never
#      printed -- the file holds the Wi-Fi password).  build_info.py can be
#      overwritten by a wrong deploy and then lie; settings.json usually is not.
#   3. optional UID pin: ~/.config/sandpico/<project>.uid lists the board UIDs
#      (from boot_out.txt) this project may be deployed to.
# Then settings.json is copied to ~/pico-settings/ (dir 700, file 600).
#
# --first-install  allow a blank board (nothing of any project on it)
# --force          deploy even though a check failed (says so loudly)
import argparse
import json
import os
import re
import sys
import time

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

BUILD_INFO = "build_info.py"
SETTINGS = "settings.json"
BOOT_OUT = "boot_out.txt"
UID_DIR = os.path.join("~", ".config", "sandpico")
UID_SUFFIX = ".uid"
BACKUP_DIR = os.path.join("~", "pico-settings")
BACKUP_DIR_MODE = 0o700
BACKUP_FILE_MODE = 0o600
STAMP_FORMAT = "%Y%m%d-%H%M%S"
UNKNOWN_UID = "unknown"
MAX_BACKUP_TRIES = 100          # same-second backups get -1, -2, ...

PROJECT_RX = re.compile(r'^\s*PROJECT\s*=\s*"([^"]*)"', re.M)
UID_RX = re.compile(r"\bUID:([0-9A-Fa-f]+)")

# What each project leaves on a board.  "files": present only on that
# project's boards.  Settings shape: sandswing has startup.program =
# concentrator.py; sandtest has (at least SANDTEST_MIN_KEYS of) its keys.
SANDSWING_PROGRAM = "concentrator.py"
SANDTEST_KEYS = ("opto_pins", "stepper", "heads", "faces")
SANDTEST_MIN_KEYS = 2
PROJECTS = {
    "sandswing": {"files": ("concentrator.py", "bell_machine.py")},
    "sandtest": {"files": ("player.py", "patterns.py", "sweep.py")},
}

BLANK = "blank"
MIXED = "mixed"


def say(msg):
    print("guard: " + msg)


def warn(msg):
    print("guard: warning: " + msg, file=sys.stderr)


def read_text(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def marker_project(dest):
    """PROJECT from the board's build_info.py, or None if not stamped."""
    text = read_text(os.path.join(dest, BUILD_INFO))
    if text is None:
        return None
    m = PROJECT_RX.search(text)
    return m.group(1).strip().lower() if m else None


def files_project(dest):
    """Which project's own files are on the board: a name, BLANK or MIXED."""
    found = [p for p, info in sorted(PROJECTS.items())
             if any(os.path.isfile(os.path.join(dest, f)) for f in info["files"])]
    if not found:
        return BLANK
    return found[0] if len(found) == 1 else MIXED


def settings_project(path):
    """Project the settings.json looks like (keys only), or None."""
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    startup = data.get("startup")
    if isinstance(startup, dict) and startup.get("program") == SANDSWING_PROGRAM:
        return "sandswing"
    if sum(1 for k in SANDTEST_KEYS if k in data) >= SANDTEST_MIN_KEYS:
        return "sandtest"
    return None


def board_uid(dest):
    text = read_text(os.path.join(dest, BOOT_OUT))
    m = UID_RX.search(text or "")
    return m.group(1).upper() if m else None


def pinned_uids(project):
    path = os.path.expanduser(os.path.join(UID_DIR, project + UID_SUFFIX))
    text = read_text(path)
    if text is None:
        return path, None
    uids = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            uids.add(line.upper())
    return path, uids


def check(project, dest, first_install):
    """Return (refusals, uid).  Each refusal is a message string."""
    refusals = []
    marker = marker_project(dest)
    files = files_project(dest)
    if marker and marker != project:
        refusals.append(
            "this board is %s (its build_info.py says PROJECT=\"%s\"), but you are "
            "deploying %s. Plug in the %s Pico." % (marker, marker, project, project))
    elif marker == project:
        say("build_info.py says %s: ok" % project)
    elif files == project:
        say("no PROJECT in build_info.py (older deploy); %s files on the board: ok" % project)
    elif files == MIXED:
        refusals.append(
            "this board has files from more than one project (%s) and no PROJECT "
            "in build_info.py, so it cannot tell which board it is. If this really "
            "is the %s Pico, run again with --force." % (", ".join(sorted(PROJECTS)), project))
    elif files != BLANK:
        refusals.append(
            "this board has %s files on it (no PROJECT in build_info.py), but you are "
            "deploying %s. Plug in the %s Pico." % (files, project, project))
    elif first_install:
        say("blank board, --first-install given: ok")
    else:
        refusals.append(
            "no %s project marker on this board (blank board or unknown contents). "
            "For a first install run again with --first-install." % BUILD_INFO)

    shape = settings_project(os.path.join(dest, SETTINGS))
    if shape and shape != project:
        refusals.append(
            "settings.json on this board looks like %s settings, but you are deploying "
            "%s. Plug in the %s Pico (or fix its settings first)." % (shape, project, project))
    elif shape == project:
        say("settings.json looks like %s: ok" % project)
    elif os.path.isfile(os.path.join(dest, SETTINGS)):
        warn("settings.json on the board is not recognisable as any project")

    uid = board_uid(dest)
    pin_path, allowed = pinned_uids(project)
    if allowed is None:
        warn("no UID pin for %s (%s)." % (project, pin_path))
        if uid and not refusals:      # never suggest pinning a board we refused
            warn("to pin this board: mkdir -p %s && echo %s >> %s"
                 % (os.path.dirname(pin_path), uid, pin_path))
    elif uid is None:
        refusals.append("%s pins the %s board UID, but this board's %s has no UID line."
                        % (pin_path, project, BOOT_OUT))
    elif uid not in allowed:
        refusals.append("board UID %s is not in %s (allowed: %s)."
                        % (uid, pin_path, ", ".join(sorted(allowed)) or "none"))
    else:
        say("board UID %s is pinned for %s: ok" % (uid, project))
    return refusals, uid


def backup_settings(project, dest, uid):
    """Copy the board's settings.json to BACKUP_DIR; return the path or None."""
    src = os.path.join(dest, SETTINGS)
    if not os.path.isfile(src):
        say("no settings.json on the board, nothing to back up")
        return None
    folder = os.path.expanduser(BACKUP_DIR)
    os.makedirs(folder, mode=BACKUP_DIR_MODE, exist_ok=True)
    os.chmod(folder, BACKUP_DIR_MODE)
    base = "%s-%s-%s" % (project, uid or UNKNOWN_UID, time.strftime(STAMP_FORMAT))
    with open(src, "rb") as f:
        data = f.read()
    for n in range(MAX_BACKUP_TRIES):
        name = base + ("-%d" % n if n else "") + ".json"
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, BACKUP_FILE_MODE)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(path, BACKUP_FILE_MODE)
        say("settings.json backed up to %s" % path)
        return path
    raise OSError("could not find a free backup name for " + base)


def main(argv=None):
    ap = argparse.ArgumentParser(description="check the board before deploying")
    ap.add_argument("--project", required=True, choices=sorted(PROJECTS))
    ap.add_argument("--dest", required=True)
    ap.add_argument("--first-install", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.dest):
        print("guard: not a directory: %s" % a.dest, file=sys.stderr)
        return EXIT_USAGE
    refusals, uid = check(a.project, a.dest, a.first_install)
    for r in refusals:
        print("guard: REFUSED: " + r, file=sys.stderr)
    if refusals and not a.force:
        print("guard: nothing was copied to %s. (--force overrides, only if you are sure.)"
              % a.dest, file=sys.stderr)
        return EXIT_REFUSED
    if refusals:
        warn("--force given: deploying %s anyway" % a.project)
    try:
        backup_settings(a.project, a.dest, uid)
    except OSError as e:
        print("guard: REFUSED: could not back up settings.json (%s); nothing was copied."
              % e.__class__.__name__, file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
