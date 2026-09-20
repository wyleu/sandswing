# pico_circuitp_async_basic_detect_quieter.py
#
# Bare-bones optical detect + light debounce (Sand* farm)
# -------------------------------------------------------
# Filename: pico_circuitp_async_basic_detect_quieter.py
#
# Overview
# --------
# Same behaviour as pico_circuitp_async_basic_detect.py, with a short
# edge hold time so console/log/MIDI are quieter near the optical threshold.
#
#   Reflective (stable) → NeoPixel RED,  Note On  vel 100, log
#   Dark (stable)       → NeoPixel GREEN, Note Off vel 0,   log
#
# Debounce: level must hold sensing.live.min_edge_ms (default 15).
# No velocity curves, no patch timeouts, no direction decode.
# Steady lasers from sensing.live.drive_duties / drive_duty.
#
# Neo: pixel 0 = status; pixel ch+1 = that channel only (other
# channel pixels are left unchanged so both heads can show state).
#
# Shared modules
# --------------
#   config_loader.py, hardware_config.py
#   sand_emit.py, sand_neo.py, sand_optical.py
#
# settings.json (excerpt)
# -----------------------
#   "startup": {
#     "program": "pico_circuitp_async_basic_detect_quieter.py"
#   },
#   "detection": { "num_bells": 2 },
#   "sensing": {
#     "live": {
#       "invert_pt": true,
#       "drive_duty": 12000,
#       "drive_duties": [12000, 12000],
#       "min_edge_ms": 5
#     }
#   },
#   "output": {
#     "enabled_formats": ["console", "log", "midi", "neopixel"]
#   }
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

DRIVE_DUTY = int(live.get("drive_duty", 20000))
INVERT_PT = bool(live.get("invert_pt", True))
MIN_EDGE_MS = int(live.get("min_edge_ms", 15))
POLL_S = 0.01

NOTE_ON_VEL = 100
NOTE_OFF_VEL = 0
COLOR_RED = (64, 0, 0)
COLOR_GREEN = (0, 64, 0)

heads = OpticalHeads.from_hw(hw, num=num, invert_pt=INVERT_PT)
neo = SandNeo.from_cfg(cfg, pins, heads.num)
open_log(cfg, suffix="basic_quiet.txt")

status_led = hw.get("status_led")
ir_pwm = hw.get("ir_pwm")

raw_duties = live.get("drive_duties")
if isinstance(raw_duties, list) and len(raw_duties) > 0:
    DRIVE_DUTIES = [int(x) for x in raw_duties]
else:
    DRIVE_DUTIES = [DRIVE_DUTY] * num
while len(DRIVE_DUTIES) < heads.num:
    DRIVE_DUTIES.append(DRIVE_DUTIES[-1] if DRIVE_DUTIES else DRIVE_DUTY)
DRIVE_DUTIES = DRIVE_DUTIES[: heads.num]

# --- MIDI ---
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
    """Set one channel pixel; do not clear the other channel."""
    if not neo or not neo.strip:
        return
    neo.strip[0] = neo.status_color
    neo.strip[ch + 1] = color
    neo.strip.show()


def neo_show_all(levels):
    """levels[i] True = reflective/red, False = dark/green."""
    if not neo or not neo.strip:
        return
    neo.strip[0] = neo.status_color
    for i in range(heads.num):
        neo.strip[i + 1] = COLOR_RED if levels[i] else COLOR_GREEN
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
    emit("ch %d REFLECTIVE NoteOn vel=%d" % (ch + 1, NOTE_ON_VEL))


def on_dark(ch):
    neo_set(ch, COLOR_GREEN)
    midi_off(ch)
    emit("ch %d DARK NoteOff vel=%d" % (ch + 1, NOTE_OFF_VEL))


async def scan_loop():
    for ch in range(heads.num):
        heads.laser_pwms[ch].duty_cycle = max(0, min(65535, DRIVE_DUTIES[ch]))

    last = [heads.read_pt(i) for i in range(heads.num)]
    candidate = [None] * heads.num
    candidate_t = [0.0] * heads.num

    neo_show_all(last)

    emit(
        "BASIC DETECT QUIETER START channels=%d duties=%s min_edge_ms=%d"
        % (heads.num, DRIVE_DUTIES, MIN_EDGE_MS)
    )

    while True:
        now = time.monotonic()
        for ch in range(heads.num):
            raw = heads.read_pt(ch)
            if raw != last[ch]:
                if candidate[ch] != raw:
                    candidate[ch] = raw
                    candidate_t[ch] = now
                elif (now - candidate_t[ch]) * 1000.0 >= MIN_EDGE_MS:
                    last[ch] = raw
                    candidate[ch] = None
                    if raw:
                        on_reflective(ch)
                    else:
                        on_dark(ch)
            else:
                candidate[ch] = None
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
    