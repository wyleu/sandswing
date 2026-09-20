# pico_circuitp_async_sandswing.py
#
# Sandswing live bell detect – reflective patch (Sand* farm)
# ----------------------------------------------------------
# Filename: pico_circuitp_async_sandswing.py
#
# Overview
# --------
# Default live behaviour for Sandswing optical heads.
#
# Rest geometry: laser on non-reflective (dark) when the bell is seated.
# When the wheel moves, a reflective patch enters the beam:
#
#   dark → reflective  : MIDI Note On
#                        velocity ∝ time spent dark since last Note Off
#                        NeoPixel channel toggles red/green
#
#   reflective → dark  : MIDI Note Off
#                        velocity ∝ duration of this reflective patch
#                        (final state is non-reflective → last event is Note Off)
#
# No asymmetric 2–1–4 direction decode yet — solid patch only.
# Optical path prove: pico_circuitp_async_optical_lineup_test.py
#
# Shared modules
# --------------
#   config_loader.py, hardware_config.py
#   sand_emit.py, sand_neo.py, sand_optical.py
#
# settings.json (excerpt)
# -----------------------
#   "startup": { "program": "pico_circuitp_async_sandswing.py" },
#   "detection": { "num_bells": 2 },
#   "pins": { "input_base": 5, "output_base": 0, "neopixel": 15 },
#   "sensing": {
#     "live": {
#       "drive_duty": 40000,
#       "min_edge_ms": 10,
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
#     "log": { "enabled": true, "filename_prefix": "sandswing_" }
#   }

import asyncio
import time
import sys

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

# live sensing options
live = {}
try:
    if cfg.raw and isinstance(cfg.raw.get("sensing"), dict):
        live = cfg.raw["sensing"].get("live") or {}
except Exception:
    live = {}

DRIVE_DUTY = int(live.get("drive_duty", 40000))
MIN_EDGE_MS = int(live.get("min_edge_ms", 10))
INVERT_PT = bool(live.get("invert_pt", True))
POLL_S = 0.005

MAX_IDLE_MS = int(live.get("max_idle_ms", 3000))
MAX_PATCH_MS = int(live.get("max_patch_ms", 2000))
DEFAULT_ON_VEL = int(live.get("default_on_vel", 64))

MIN_PATCH_MS = int(live.get("min_patch_ms", 80))
MIN_GAP_MS = int(live.get("min_gap_ms", 100))

heads = OpticalHeads.from_hw(hw, num=num, invert_pt=INVERT_PT)
neo = SandNeo.from_cfg(cfg, pins, heads.num)
open_log(cfg, suffix="live.txt")

status_led = hw.get("status_led")
ir_pwm = hw.get("ir_pwm")

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
            print(
                "MIDI ch %d note_base %d"
                % (midi_channel + 1, note_base)
            )
    except Exception as e:
        print("MIDI init failed:", e)
        midi = None

def map_ms_to_vel(ms, ms_lo=20, ms_hi=800, vel_lo=20, vel_hi=127):
    """Longer duration → higher velocity; clamp."""
    if ms <= ms_lo:
        return vel_lo
    if ms >= ms_hi:
        return vel_hi
    ratio = (ms - ms_lo) / float(ms_hi - ms_lo)
    return int(vel_lo + ratio * (vel_hi - vel_lo))

# Per-channel state
# state: "idle" (dark / non-reflective) | "active" (reflective)
states = ["idle"] * heads.num
t_on = [0.0] * heads.num
last_off_time = [time.monotonic()] * heads.num
last_raw = [heads.read_pt(i) for i in range(heads.num)]
candidate = [None] * heads.num   # pending level while debouncing
candidate_t = [0.0] * heads.num

def neo_idle_all():
    if neo and neo.strip:
        neo.strip[0] = neo.status_color
        for i in range(1, len(neo.strip)):
            neo.strip[i] = (0, 0, 0)
        neo.strip.show()

def neo_toggle(ch):
    if neo:
        neo.toggle_channel(ch)

def midi_on(ch, vel):
    if not midi:
        return
    note = note_base + ch
    vel = max(1, min(127, int(vel)))
    try:
        from adafruit_midi.note_on import NoteOn
        midi.send(NoteOn(note, vel))
    except Exception as e:
        emit("MIDI NoteOn fail: %s" % e)

def midi_off(ch, vel):
    if not midi:
        return
    note = note_base + ch
    vel = max(0, min(127, int(vel)))
    try:
        # Prefer NoteOff with velocity if supported; else velocity 0
        from adafruit_midi.note_off import NoteOff
        midi.send(NoteOff(note, vel))
    except Exception as e:
        emit("MIDI NoteOff fail: %s" % e)

def on_enter_reflective(ch, now):
    gap_ms = (now - last_off_time[ch]) * 1000.0
    
    if gap_ms < MIN_GAP_MS:
        return  # ignore bounce back into reflective
    
    if gap_ms > MAX_IDLE_MS:
        vel_on = DEFAULT_ON_VEL
    else:
        vel_on = map_ms_to_vel(gap_ms)

    t_on[ch] = now
    states[ch] = "active"
    midi_on(ch, vel_on)
    neo_toggle(ch)
    emit(
        "ch %d NOTE ON  gap_dark=%.0f ms vel=%d"
        % (ch + 1, gap_ms, vel_on)
    )

def on_leave_reflective(ch, now):
    patch_ms = (now - t_on[ch]) * 1000.0

    if patch_ms < MIN_PATCH_MS:
        # treat as glitch: go idle without MIDI, or still off if you already sent on
        states[ch] = "idle"
        last_off_time[ch] = now
        return
    
    vel_off = map_ms_to_vel(patch_ms)
    last_off_time[ch] = now
    states[ch] = "idle"
    midi_off(ch, vel_off)
    emit(
        "ch %d NOTE OFF patch=%.0f ms vel=%d"
        % (ch + 1, patch_ms, vel_off)
    )

async def scan_loop():
    """Steady laser; debounced dark/reflective edges → MIDI + neo."""
    # All lasers on steady (path ready for wheel motion)
    for ch in range(heads.num):
        heads.laser_duty(ch, DRIVE_DUTY)

    emit(
        "LIVE START family=%s channels=%d duty=%d min_edge_ms=%d"
        % (cfg.family, heads.num, DRIVE_DUTY, MIN_EDGE_MS)
    )
    neo_idle_all()

    while True:
        now = time.monotonic()
        for ch in range(heads.num):
            now = time.monotonic()  # or use outer now
            raw = heads.read_pt(ch)

            # --- force Note Off if reflective too long (no return to dark) ---
            if states[ch] == "active":
                if (now - t_on[ch]) * 1000.0 >= MAX_PATCH_MS:
                    emit("ch %d TIMEOUT patch → force NOTE OFF" % (ch + 1,))
                    on_leave_reflective(ch, now)
                    last_raw[ch] = heads.read_pt(ch)
                    candidate[ch] = None
                    continue

            # --- debounce edges ---
            if raw != last_raw[ch]:
                if candidate[ch] != raw:
                    candidate[ch] = raw
                    candidate_t[ch] = now
                elif (now - candidate_t[ch]) * 1000.0 >= MIN_EDGE_MS:
                    last_raw[ch] = raw
                    candidate[ch] = None
                    reflective = bool(raw)

                    if states[ch] == "idle" and reflective:
                        on_enter_reflective(ch, now)
                    elif states[ch] == "active" and not reflective:
                        on_leave_reflective(ch, now)
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
    # Note off any active channels
    for ch in range(heads.num):
        if states[ch] == "active":
            on_leave_reflective(ch, time.monotonic())
    if neo:
        neo.all_off()
    close_log()
    print("Cleanup complete")