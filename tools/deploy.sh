#!/bin/bash
set -euo pipefail
SRC="$HOME/Code/Sandbells/sandswing/src"
need=(boot.py code.py sandswing.py pins_from_settings.py
      config_loader.py farm_log.py farm_ws.py settings.json)

if [ ! -d "$SRC" ]; then
  echo "FAIL: no $SRC"
  exit 1
fi
for f in "${need[@]}"; do
  if [ ! -f "$SRC/$f" ]; then
    echo "FAIL: missing $SRC/$f"
    exit 1
  fi
done
if [ ! -d "$SRC/lib" ]; then
  echo "FAIL: missing $SRC/lib"
  exit 1
fi

PICO=""
for d in /media/"$USER"/CIRCUITPY /media/"$USER"/CIRCUITPY1 /media/"$USER"/RP2350; do
  if [ -d "$d" ]; then PICO="$d"; break; fi
done
if [ -z "$PICO" ]; then
  # last resort: the 2.5MB generic label (sick or freshly labelled)
  for d in /media/"$USER"/*; do
    [ -d "$d" ] || continue
    case "$(basename "$d")" in
      CIRCUITPY*|RP2350|RPI-RP2) PICO="$d"; break ;;
    esac
  done
fi

if [ -z "$PICO" ]; then
  echo "FAIL: no CIRCUITPY/RP2350 mounted under /media/$USER"
  ls /media/"$USER" 2>/dev/null || true
  exit 1
fi

if [ "$(basename "$PICO")" = "RP2350" ] || [ "$(basename "$PICO")" = "RPI-RP2" ]; then
  echo "FAIL: $PICO is the bootloader. Copy a UF2 there, not src/."
  exit 1
fi

touch "$PICO/.write_test" 2>/dev/null || {
  echo "FAIL: $PICO is not writable (read-only FAT). Format with UF2 first."
  exit 1
}
rm -f "$PICO/.write_test"

echo "Deploy $SRC -> $PICO"
cp "$SRC/boot.py" "$SRC/code.py" "$SRC/sandswing.py" \
   "$SRC/pins_from_settings.py" "$SRC/config_loader.py" \
   "$SRC/farm_log.py" "$SRC/farm_ws.py" "$SRC/settings.json" \
   "$PICO/"
cp -a "$SRC/lib" "$PICO/"
sync
echo "OK. On Pico:"
ls "$PICO"
echo "Eject, unplug, plug in without BOOTSEL."
