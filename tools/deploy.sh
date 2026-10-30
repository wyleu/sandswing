#!/bin/bash
# Stamp CIRCUITPY from ../src/. Does not nuke. lib/ only with --lib.
# Optional --settings copies settings.json as well.
#
# Before copying anything, tools/board_guard.py checks that the board really
# is a sandswing Pico (build_info.py PROJECT, settings.json shape, optional
# UID pin in ~/.config/sandpico/sandswing.uid) and backs up the board's
# settings.json to ~/pico-settings/.  It refuses otherwise.
#   --first-install   allow a blank board
#   --force           deploy even though the guard refused
#   --dest DIR        use this mounted drive instead of finding $PICO_LABEL
#   (or just DIR)     (not unmounted afterwards)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/src"
LABEL="${PICO_LABEL:-CIRCUITPY}"
PROJECT="sandswing"
WITH_LIB=0
WITH_SETTINGS=0
DEST=""
GUARD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lib) WITH_LIB=1 ;;
    --settings) WITH_SETTINGS=1 ;;
    --force|--first-install) GUARD_ARGS+=("$1") ;;
    --dest)
      if [[ $# -lt 2 ]]; then
        echo "--dest needs a directory" >&2
        exit 2
      fi
      DEST="$2"
      shift ;;
    -*)
      echo "unknown argument: $1" >&2
      echo "usage: tools/deploy.sh [--settings] [--lib] [--first-install] [--force] [--dest DIR]" >&2
      exit 2 ;;
    *) DEST="$1" ;;              # tools/deploy.sh /media/$USER/CIRCUITPY, like sandtest
  esac
  shift
done

if [[ ! -d "$SRC" ]]; then
  echo "no src dir: $SRC" >&2
  exit 1
fi

HASH="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# a "+" says the files differ from that commit (uncommitted edits)
git -C "$ROOT" diff --quiet HEAD -- src 2>/dev/null || HASH="$HASH+"
DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'GIT="%s"\nBUILT="%s"\nPROJECT="%s"\n' "$HASH" "$DATE" "$PROJECT" > "$SRC/build_info.py"
echo "build $HASH $DATE"

LIST="$ROOT/tools/files.txt"
mapfile -t FILES < <(grep -v '^[[:space:]]*#' "$LIST" | grep -v '^[[:space:]]*$')
for f in "${FILES[@]}" build_info.py; do
  if [[ ! -f "$SRC/$f" ]]; then
    echo "missing $SRC/$f" >&2
    exit 1
  fi
done

PY=()
for f in "${FILES[@]}"; do
  [[ "$f" == *.py ]] && PY+=("$SRC/$f")
done
python3 -m py_compile "$SRC/build_info.py" "${PY[@]}"
python3 -m json.tool "$SRC/settings.json" >/dev/null

if [[ -n "$DEST" ]]; then
  if [[ ! -d "$DEST" ]]; then
    echo "not a directory: $DEST (is the Pico plugged in and mounted?)" >&2
    exit 1
  fi
  MP="$DEST"
  DEV=""
elif [[ -e /dev/disk/by-label/$LABEL ]]; then
  DEV="$(readlink -f /dev/disk/by-label/$LABEL)"
else
  echo "no disk labelled $LABEL (plug the Pico, wait 3s)" >&2
  lsblk -o NAME,LABEL,MOUNTPOINT
  exit 1
fi

if [[ -n "$DEV" ]]; then
  MP="$(lsblk -npo MOUNTPOINT "$DEV")"
  if [[ -z "${MP:-}" ]]; then
    udisksctl mount -b "$DEV" >/dev/null
    MP="$(lsblk -npo MOUNTPOINT "$DEV")"
  fi
  if [[ -z "${MP:-}" ]]; then
    echo "could not mount $DEV" >&2
    exit 1
  fi
fi

# wrong-board guard + settings backup; nothing has been copied yet
python3 "$ROOT/tools/board_guard.py" --project "$PROJECT" --dest "$MP" ${GUARD_ARGS[@]+"${GUARD_ARGS[@]}"}

echo "stamp $SRC -> $MP (${DEV:-$MP}) $HASH"
for f in "${FILES[@]}" build_info.py; do
  cp "$SRC/$f" "$MP/$f"
done

if [[ "$WITH_SETTINGS" -eq 1 ]]; then
  cp "$SRC/settings.json" "$MP/settings.json"
  echo "settings.json copied"
fi

if [[ "$WITH_LIB" -eq 1 ]]; then
  mkdir -p "$MP/lib"
  cp -a "$SRC/lib/." "$MP/lib/"
  echo "lib/ copied"
fi

sync
if [[ -n "$DEV" ]]; then
  udisksctl unmount -b "$DEV"
  echo "unmounted $DEV — mpremote connect auto, then Ctrl-D"
else
  echo "copied to $MP (left mounted) — boot line: SANDSWING $HASH $DATE"
fi