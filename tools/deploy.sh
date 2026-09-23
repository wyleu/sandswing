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

python3 -m json.tool "$SRC/settings.json" >/dev/null
python3 -m py_compile \
  "$SRC/boot.py" "$SRC/code.py" "$SRC/sandswing.py" \
  "$SRC/pins_from_settings.py" "$SRC/bell.py" "$SRC/mux4051.py" \
  "$SRC/sand_status.py" "$SRC/config_loader.py" \
  "$SRC/farm_log.py" "$SRC/farm_ws.py"

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

echo "stamp $SRC -> $MP ($DEV)"
cp "$SRC/boot.py" "$SRC/code.py" "$SRC/sandswing.py" \
   "$SRC/pins_from_settings.py" "$SRC/bell.py" "$SRC/mux4051.py" \
   "$SRC/sand_status.py" "$SRC/config_loader.py" \
   "$SRC/farm_log.py" "$SRC/farm_ws.py" \
   "$SRC/settings.json" \
   "$MP/"

if [[ "$WITH_LIB" -eq 1 ]]; then
  mkdir -p "$MP/lib"
  cp -a "$SRC/lib/." "$MP/lib/"
  echo "lib/ copied"
fi

sync
udisksctl unmount -b "$DEV"
echo "unmounted $DEV — mpremote connect auto, then Ctrl-D"
