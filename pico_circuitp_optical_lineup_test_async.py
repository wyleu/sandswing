# pico_circuitp_async_optical_lineup_test.py
#
# Optical path lineup test (Sand* farm)
# -------------------------------------
# Filename: pico_circuitp_async_optical_lineup_test.py
#
# Overview
# --------
# Proves laser → reflector → phototransistor on each head in turn.
#
# For each channel:
#   • Steady or pulsed laser at sensing.test.drive_duty / drive_duties
#   • Poll PT; on level change → console, log, neo R/G on that channel
#   • Dwell sensing.test.dwell_s then next channel; repeat
#
# Default pulse: 48/min (interval 0.625 s on, 0.625 s off) so the beam
# is obvious. PT edges still reported whenever they occur.
#
# NOT live Sandswing. NOT the broken sweep/merge of the old lineup file.
# Live detect: pico_circuitp_async_basic_detect.py
#
# Shared modules
# --------------
#   config_loader.py, hardware_config.py
#   sand_emit.py, sand_neo.py, sand_optical.py
#
# settings.json (excerpt)
# -----------------------
#   "startup": {
#     "program": "pico_circuitp_async_optical_lineup_test.py"
#   },
#   "detection": { "num_bells": 2 },
#   "sensing": {
#     "test": {
#       "drive_duty": 15000,
#       "drive_duties": [15000, 15000],
#       "dwell_s": 10.0,
#       "tick_interval_s": 0.625,
#       "channel": null,
#       "invert_pt": true
#     },
#     "live": {
#       "invert_pt": true
#     }
#   },
#   "output": {
#     "enabled_formats": ["console", "log", "neopixel"]
#   }
#
# Lineup
# ------
# 1. Point startup.program at this file; run.
# 2. Watch which physical laser pulses during "ch 1" / "ch 2".
# 3. Block/clear that beam → CHANGE lines + neo on that pixel.
# 4. Switch startup back to basic_detect when done.

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

test = {}
live = {}
try:
    if cfg.raw and isinstance(cfg.raw.get("sensing"), dict):
        test = cfg.raw["sensing"].get("test") or {}
        live = cfg.raw["sensing"].get("live") or {}
except Exception:
    pass

INVERT_PT = bool(test.get("invert_pt", live.get("invert_pt", True)))
DRIVE_DUTY = int(test.get("drive_duty", live.get("drive_duty", 15000)))
DWELL_S = float(test.get("dwell_s", 10.0))
TICK_INTERVAL_S = float(test.get("tick_interval_s", 0.625))
POLL_S = 0.01

raw_duties = test.get("drive_duties") or live.get("drive_duties")
if isinstance(raw_duties, list) and len(raw_duties) > 0:
    DRIVE_DUTIES = [int(x) for x in raw_duties]
else:
    DRIVE_DUTIES = [DRIVE_DUTY] * num
while len(DRIVE_DUTIES) < num:
    DRIVE_DUTIES.append(DRIVE_DUTIES[-1] if DRIVE_DUTIES else DRIVE_DUTY)
DRIVE_DUTIES = DRIVE_DUTIES[:num]

_ch = None
raw_ch = test.get("channel")
if raw_ch is not None and raw_ch != "":
    try:
        _ch = int(raw_ch)
    except Exception:
        _ch = None

COLOR_RED = (64, 0, 0)
COLOR_GREEN = (0, 64, 0)

heads = OpticalHeads.from_hw(hw, num=num, invert_pt=INVERT_PT)
neo = SandNeo.from_cfg(cfg, pins, heads.num)
open_log(cfg, suffix="lineup.txt")

status_led = hw.get("status_led")
ir_pwm = hw.get("ir_pwm")


def neo_set(ch, color):
    if not neo or not neo.strip:
        return
    neo.strip[0] = neo.status_color
    neo.strip[ch + 1] = color
    neo.strip.show()


def neo_idle_channel(ch):
    if not neo or not neo.strip:
        return
    neo.strip[0] = neo.status_color
    for i in range(1, len(neo.strip)):
        neo.strip[i] = (0, 0, 0)
    neo.strip[ch + 1] = COLOR_GREEN
    neo.strip.show()


async def test_channel(ch):
    duty = DRIVE_DUTIES[ch]
    emit(
        "--- ch %d %s duty=%d pulse=%.3fs ---"
        % (ch + 1, heads.describe(ch), duty, TICK_INTERVAL_S)
    )
    neo_idle_channel(ch)

    # other heads off; this head pulsed
    heads.all_off()
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
            if phase:
                heads.laser_pwms[ch].duty_cycle = max(0, min(65535, duty))
                ticks += 1
                emit("ch %d TICK #%d laser=ON" % (ch + 1, ticks))
            else:
                heads.laser_pwms[ch].duty_cycle = 0
                emit("ch %d laser=OFF" % (ch + 1,))
            last_toggle = now

        raw = heads.read_pt(ch)
        if raw != last_pt:
            last_pt = raw
            changes += 1
            if raw:
                neo_set(ch, COLOR_RED)
                emit("ch %d CHANGE → light/return PT=%s" % (ch + 1, raw))
            else:
                neo_set(ch, COLOR_GREEN)
                emit("ch %d CHANGE → no return PT=%s" % (ch + 1, raw))

        await asyncio.sleep(POLL_S)

    heads.all_off()
    emit("ch %d done ticks=%d changes=%d" % (ch + 1, ticks, changes))
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
        "LINEUP START family=%s channels=%d duties=%s"
        % (cfg.family, heads.num, DRIVE_DUTIES)
    )

    channels = list(range(heads.num))
    if _ch is not None:
        if 0 <= _ch < heads.num:
            channels = [_ch]
        else:
            emit("invalid sensing.test.channel=%s – all channels" % (_ch,))

    while True:
        totals = []
        for ch in channels:
            totals.append(await test_channel(ch))
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