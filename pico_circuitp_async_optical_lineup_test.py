# pico_circuitp_async_optical_lineup_test.py
#
# Optical path test – fixed drive (Sand* farm)
# --------------------------------------------
# Filename: pico_circuitp_async_optical_lineup_test.py
#
# Overview
# --------
# Prescribed bench test: turn laser ON at a fixed duty, watch the
# phototransistor for a reflector return, report changes on console/log
# and toggle NeoPixel red/green (sensor-alive feedback).
#
# This is NOT a PWM sensitivity sweep and NOT live Sandswing behaviour.
# Default live app: pico_circuitp_async_sandswing.py
#
# Behaviour
# ---------
# For each channel (or one channel from settings):
#   1. Select channel on NeoPixel
#   2. Laser ON at sensing.test.drive_duty
#   3. Poll PT; on change → emit + neo R/G toggle
#   4. After dwell_s → laser OFF, next channel (or loop)
#
# Shared modules
# --------------
#   config_loader.py   – settings.json
#   hardware_config.py – pins / digital inputs
#   sand_emit.py       – console + log
#   sand_neo.py        – status + per-channel R/G
#   sand_optical.py    – laser PWM + read_pt
#
# settings.json (excerpt)
# -----------------------
#   "startup": {
#     "program": "pico_circuitp_async_optical_lineup_test.py"
#   },
#   "detection": { "num_bells": 2 },
#   "pins": { "input_base": 5, "output_base": 0, "neopixel": 15 },
#   "sensing": {
#     "test": {
#       "drive_duty": 40000,
#       "dwell_s": 10.0,
#       "channel": null
#     }
#   },
#   "output": {
#     "enabled_formats": ["console", "log", "neopixel"],
#     "log": { "enabled": true, "filename_prefix": "sandswing_" }
#   }
#
# Lineup
# ------
#   Reflective target in beam → CHANGE lines + neo R/G while laser is on.
#   Aim until changes are clean; then switch startup.program to live app.

import asyncio
import time

from config_loader import load_config
from hardware_config import create_hardware
from sand_emit import open_log, close_log, emit
from sand_neo import SandNeo
from sand_optical import OpticalHeads

# ---------------------------------------------------------------------------
cfg = load_config()
cfg.banner()

hw = create_hardware(cfg)
pins = hw["pins"]
det = hw["detection"]

num = int(det.get("num_bells", 2))
heads = OpticalHeads.from_hw(hw, num=num, invert_pt=True)
neo = SandNeo.from_cfg(cfg, pins, heads.num)

DRIVE_DUTY = cfg.get_int("sensing.test.drive_duty", 40000)
DWELL_S = cfg.get_float("sensing.test.dwell_s", 10.0)
# null / missing → all channels; 0-based index for one channel only
_ch = None
try:
    raw_ch = None
    if cfg.raw and isinstance(cfg.raw.get("sensing"), dict):
        raw_ch = (cfg.raw["sensing"].get("test") or {}).get("channel")
    if raw_ch is not None and raw_ch != "":
        _ch = int(raw_ch)
except Exception:
    _ch = None

open_log(cfg, suffix="lineup.txt")

status_led = hw.get("status_led")
ir_pwm = hw.get("ir_pwm")

print(
    "Optical path test  channels=%d drive_duty=%d dwell_s=%.1f"
    % (heads.num, DRIVE_DUTY, DWELL_S)
)


# 48 ticks per minute
TICK_INTERVAL_S = cfg.get_float("sensing.test.tick_interval_s", 0.625)
# 0.625 on + 0.625 off ≈ 48 cycles/min if each full cycle counts as one flash pair;
# one edge every 0.625 s → 96 edges/min; one ON event every 1.25 s → 48/min.
# Use: raise tick on each transition to ON → 48/min when interval is 0.625.

DRIVE_DUTY = cfg.get_int("sensing.test.drive_duty", 40000)
DWELL_S = cfg.get_float("sensing.test.dwell_s", 10.0)

async def test_path(ch):
    emit(
        "--- path test ch %d %s duty=%d 48/min ---"
        % (ch + 1, heads.describe(ch), DRIVE_DUTY)
    )
    if neo and neo.strip:
        neo.strip[0] = neo.status_color
        for i in range(1, len(neo.strip)):
            neo.strip[i] = (0, 0, 0)
        neo.strip.show()

    last_pt = heads.read_pt(ch)
    phase = False
    ticks = 0
    changes = 0
    t0 = time.monotonic()
    last_toggle = t0

    while (time.monotonic() - t0) < DWELL_S:
        now = time.monotonic()

        if now - last_toggle >= TICK_INTERVAL_S:
            phase = not phase
            heads.laser_on(ch, phase, duty=DRIVE_DUTY)
            last_toggle = now

            if phase:
                # once per laser-ON only
                ticks += 1
                emit("ch %d TICK #%d laser=ON" % (ch + 1, ticks))
                if neo and neo.strip:
                    neo.strip[0] = neo.status_color  # always green status
                    on_color = neo.color_b if (ticks % 2) else neo.color_a
                    for i in range(1, len(neo.strip)):
                        neo.strip[i] = on_color if (i == ch + 1) else (0, 0, 0)
                    neo.strip.show()

        raw = heads.read_pt(ch)
        if raw != last_pt:
            last_pt = raw
            changes += 1
            emit("ch %d CHANGE #%d PT=%s" % (ch + 1, changes, raw))

        await asyncio.sleep(0.01)

    heads.all_off()
    if neo and neo.strip:
        neo.strip[0] = neo.status_color
        for i in range(1, len(neo.strip)):
            neo.strip[i] = (0, 0, 0)
        neo.strip.show()

    emit("ch %d done ticks=%d PT_changes=%d" % (ch + 1, ticks, changes))
    return changes

async def main():
    if status_led:
        status_led.value = True
    if ir_pwm:
        try:
            ir_pwm.duty_cycle = 0
        except Exception:
            pass

    heads.all_off()
    emit(
        "PATH TEST START family=%s name=%s"
        % (cfg.family, cfg.device_name)
    )

    channels = list(range(heads.num))
    if _ch is not None:
        if 0 <= _ch < heads.num:
            channels = [_ch]
        else:
            emit("invalid sensing.test.channel=%s – using all" % (_ch,))

    while True:
        totals = []
        for ch in channels:
            totals.append(await test_path(ch))
        emit("PASS changes per ch: %s" % (totals,))


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped")
finally:
    heads.all_off()
    if neo:
        neo.all_off()
    close_log()
    print("Cleanup complete")