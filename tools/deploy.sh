#!/bin/bash
set -euo pipefail
SRC="$HOME/Code/Sandbells/sandswing/src"
need=(boot.py code.py sandswing.py pins_from_settings.py
      config_loader.py farm_log.py farm_ws.py settings.json)

die() { echo "FAIL: $*"; exit 1; }

[ -d "$SRC" ] || die "no $SRC"
for f in "${need[@]}"; do
  [ -f "$SRC/$f" ] || die "missing $SRC/$f"
done
[ -d "$SRC/lib" ] || die "missing $SRC/lib"
command -v mpremote >/dev/null || die "mpremote not installed"
[ -e /dev/ttyACM0 ] || [ -e /dev/ttyACM1 ] || die "no /dev/ttyACM* — plug Pico in (not BOOTSEL)"

if ! mpremote connect auto exec "print('ping')" >/tmp/mp-ping.txt 2>&1; then
  cat /tmp/mp-ping.txt
  die "serial busy. Close Thonny, screen, and other mpremote."
fi

echo "Copying…"
for f in "${need[@]}"; do
  echo "  $f"
  mpremote connect auto cp "$SRC/$f" ":$f"
done
echo "  lib/"
mpremote connect auto cp -r "$SRC/lib" :
echo "Reset…"
mpremote connect auto reset || true
echo "OK —  mpremote connect auto"
