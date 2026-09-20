# pico_circuitp_async_basic_detect.py
#
# Bare-bones optical detect (Sand* farm)
# --------------------------------------
# Filename: pico_circuitp_async_basic_detect.py
#
# Overview
# --------
# Absolute minimum reflective-patch detector for Sandswing heads.
#
#   Reflective edge  → NeoPixel RED, console, log, MIDI Note On  vel 100
#   Leave reflective → NeoPixel GREEN, console, log, MIDI Note Off vel 0
#
# No debounce, no velocity curves, no timeouts, no direction decode.
# Steady laser at a fixed duty. One poll loop.
#
# Use this to prove: laser → reflector → PT → emit path.
# Richer live behaviour: pico_circuitp_async_sandswing.py
# 48/min path test:     pico_circuitp_async_optical_lineup_test.py
#
# Shared modules
# --------------
#   config_loader.py, hardware_config.py
#   sand_emit.py, sand_neo.py, sand_optical.py
#
# settings.json (excerpt)
# -----------------------
#   "startup": {
#     "program": "pico_circuitp_async_basic_detect.py"
#   },
#   "detection": { "num_bells": 2 },
#   "pins": {
#     "input_base": 5,
#     "output_base": 0,
#     "neopixel": 15
#   },
#   "sensing": {
#     "live": {
#       "drive_duty": 40000,
#       "invert_pt": true
#     }
#   },
#   "output": {
#     "enabled_formats": ["console", "log", "midi", "neopixel"],
#     "midi": {
#       "enabled": true,
#       "channel": 0,
#       "note_base": 60
#     },
#     "log": {
#       "enabled": true,
#       "filename_prefix": "sandswing_"
#     },
#     "neopixel": {
#       "enabled": true,
#       "brightness": 0.25,
#       "status_color": [0, 32, 0],
#       "pixel_order": "GRB"
#     }
#   }
#
# Hardware (typical Sandswing, 2 heads)
# -------------------------------------
#   Laser PWM  GP0, GP1   (output_base + ch)
#   PT digital GP5, GP6   (input_base + ch)
#   NeoPixel   GP15       pixel 0 = status, 1..N = channels
#
# Launch
# ------
#   boot.py → code.py → settings.json → this file

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

live = {}
try:
    if cfg.raw and isinstance(cfg.raw.get("sensing"), dict):
        live = cfg.raw["sensing"].get("live") or {}
except Exception:
    pass

DRIVE_DUTY = int(live.get("drive_duty", 40000))
INVERT_PT = bool(live.get("invert_pt", True))
POLL_S = 0.01

NOTE_ON_VEL = 100
NOTE_OFF_VEL = 0
COLOR_RED = (64, 0, 0)
COLOR_GREEN = (0, 64, 0)

heads = OpticalHeads.from_hw(hw, num=num, invert_pt=INVERT_PT)

DRIVE_DUTY = int(live.get("drive_duty", 20000))
raw_duties = live.get("drive_duties")
if isinstance(raw_duties, list) and len(raw_duties) > 0:
    DRIVE_DUTIES = [int(x) for x in raw_duties]
else:
    DRIVE_DUTIES = [DRIVE_DUTY] * num
# pad / trim to heads.num
while len(DRIVE_DUTIES) < heads.num:
    DRIVE_DUTIES.append(DRIVE_DUTIES[-1] if DRIVE_DUTIES else DRIVE_DUTY)
DRIVE_DUTIES = DRIVE_DUTIES[: heads.num]



neo = SandNeo.from_cfg(cfg, pins, heads.num)
open_log(cfg, suffix="basic.txt")

status_led = hw.get("status_led")
ir_pwm = hw.get("ir_pwm")

# --- MIDI (optional) ---
midi = None
midi_channel = 0
note_base = 60
if cfg.format_enabled("midi"):
    try:
        import usb_midi
        import adafruit_midi
        from adafruit_midi.note_on import NoteOn
        from adafruit_midi.note_off import NoteOff

        midi_conf = (cfg.output.get("midi") if cfg.output else None) or {}
        if midi_conf.get("enabled", True):
            midi_channel = int(midi_conf.get("channel", 0))
            note_base = int(midi_conf.get("note_base", 60))
            midi = adafruit_midi.MIDI(
                midi_out=usb_midi.ports[1],
                out_channel=midi_channel,
            )
            print("MIDI ch %d note_base %d" % (midi_channel + 1, note_base))
    except Exception as e:
        print("MIDI init failed:", e)
        midi = None


def neo_set(ch, color):
    if not neo or not neo.strip:
        return
    neo.strip[0] = neo.status_color
    for i in range(1, len(neo.strip)):
        neo.strip[i] = color if (i == ch + 1) else (0, 0, 0)
    neo.strip.show()


def midi_on(ch):
    if not midi:
        return
    try:
        from adafruit_midi.note_on import NoteOn
        midi.send(NoteOn(note_base + ch, NOTE_ON_VEL))
    except Exception as e:
        emit("MIDI NoteOn fail: %s" % e)


def midi_off(ch):
    if not midi:
        return
    try:
        from adafruit_midi.note_off import NoteOff
        midi.send(NoteOff(note_base + ch, NOTE_OFF_VEL))
    except Exception as e:
        emit("MIDI NoteOff fail: %s" % e)


def on_reflective(ch):
    neo_set(ch, COLOR_RED)
    midi_on(ch)
    emit("ch %d REFLECTIVE  NoteOn vel=%d" % (ch + 1, NOTE_ON_VEL))


def on_dark(ch):
    neo_set(ch, COLOR_GREEN)
    midi_off(ch)
    emit("ch %d DARK  NoteOff vel=%d" % (ch + 1, NOTE_OFF_VEL))


async def scan_loop():
    heads.lasers_duties(DRIVE_DUTIES)

    last = [heads.read_pt(i) for i in range(heads.num)]
    # Align neo to current level (no event spam at boot)
    for ch in range(heads.num):
        if last[ch]:
            neo_set(ch, COLOR_RED)
        else:
            neo_set(ch, COLOR_GREEN)

    emit(
        "BASIC DETECT START family=%s channels=%d duty=%d"
        % (cfg.family, heads.num, DRIVE_DUTY)
    )

    while True:
        for ch in range(heads.num):
            raw = heads.read_pt(ch)
            if raw != last[ch]:
                last[ch] = raw
                if raw:
                    on_reflective(ch)
                else:
                    on_dark(ch)
        await asyncio.sleep(POLL_S)


async def main():
    if status_led:
        status_led.value = True
    if ir_pwm:
        try:
            ir_pwm.duty_cycle = 0
        except Exception:
            pass
    await scan_loop()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped")
finally:
    heads.all_off()
    for ch in range(heads.num):
        if midi:
            midi_off(ch)
    if neo:
        neo.all_off()
    close_log()
    print("Cleanup complete")