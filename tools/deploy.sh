#!/bin/bash
# Stamp CIRCUITPY from ../src/. Does not nuke. lib/ only with --lib.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/src"
LABEL="${PICO_LABEL:-CIRCUITPY}"
WITH_LIB=0
[[ "${1:-}" == "--lib" ]] && WITH_LIB=1

if [[ ! -d "$SRC" ]]; then
  echo "no src dir: $SRC" >&2
  exit 1
fi

HASH="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'GIT="%s"\nBUILT="%s"\n' "$HASH" "$DATE" > "$SRC/build_info.py"
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

if [[ -e /dev/disk/by-label/$LABEL ]]; then
  DEV="$(readlink -f /dev/disk/by-label/$LABEL)"
else
  echo "no disk labelled $LABEL (plug the Pico, wait 3s)" >&2
  lsblk -o NAME,LABEL,MOUNTPOINT
  exit 1
fi

MP="$(lsblk -npo MOUNTPOINT "$DEV")"
if [[ -z "${MP:-}" ]]; then
  udisksctl mount -b "$DEV" >/dev/null
  MP="$(lsblk -npo MOUNTPOINT "$DEV")"
fi
if [[ -z "${MP:-}" ]]; then
  echo "could not mount $DEV" >&2
  exit 1
fi

echo "stamp $SRC -> $MP ($DEV) $HASH"
for f in "${FILES[@]}" build_info.py; do
  cp "$SRC/$f" "$MP/$f"
done

if [[ "$WITH_LIB" -eq 1 ]]; then
  mkdir -p "$MP/lib"
  cp -a "$SRC/lib/." "$MP/lib/"
  echo "lib/ copied"
fi

sync
udisksctl unmount -b "$DEV"
echo "unmounted $DEV — mpremote connect auto, then Ctrl-D"
