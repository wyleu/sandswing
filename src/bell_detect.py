"""
bell_detect.py
==============
CircuitPython runtime that *feeds* bell_machine.BellMachine.

Filename: bell_detect.py
Board:    Raspberry Pi Pico 2 W  (same stamp as sandswing)
Launch:   settings.json → startup.program  (do not point at this
          until the machine + stand pins are wired and tested)

WHAT THIS PROGRAM DOES
    1. load_config / load_pinmap / claim hardware (sense + laser PWM
       + NeoPixel + optional 4051). Park each fitted laser at a *capped*
       duty from settings — never the 0–100% lineup sweep, never 38 kHz
       on GP13 (that was the historic IR carrier).
    2. Read per-bell wheel PT and the two stand PTs.
    3. Optional: turn the 7-unit shine/dark stream into pattern="cw"|"acw".
    4. Call BellMachine.update(...) at SCAN_INTERVAL_MS.
    5. Emit state names + events on console / farm_log / WS / MIDI.
       The room sees IDLE / MOVING_CW / STOOD_HAND, not "GP0 went low".

WHAT THIS PROGRAM DOES NOT DO
    - Does not invent hand vs back with no stand strip and no direction.
    - Does not run sandswing's PWM sweep.
    - Does not use hardware_config.create_hardware or INPUT_BASE=14.
    - Does not decode audio strike (later event into the same machine).

PINS
    Same settings.json "pins" block as lineup:
        sense / laser_pwm / neopixel / fitted
        mux adc / a / b / c
    Stand strips need two extra sense GPIOs in settings when they exist.
    Until those keys exist, boot can only assert STOOD vs IDLE, not side.

RELATION TO HISTORY
    pico_circuitp_8bell_scan_rs_long_midi_latch.py is the ancestor
    (latch + middle-gap + MIDI). Keep it in the repo. Do not copy it
    onto CIRCUITPY; its GP0–7 outputs collide with this loom.
"""
import asyncio
import time

from config_loader import load_config
from pins_from_settings import load_pinmap, claim_lineup_hardware
from bell import Bell
from mux4051 import Mux4051
from bell_machine import BellMachine
import farm_log

try:
    from sand_status import StatusStrip
except Exception:
    StatusStrip = None


def _cap_duty(cfg):
    try:
        return int(cfg.get_int("sensing.laser.max_duty", 22000))
    except Exception:
        return 22000


def _stand_flags(pinmap):
    # Optional until settings grows stand_hand / stand_back GPIOs.
    return False, False


def setup():
    cfg = load_config()
    if hasattr(cfg, "banner"):
        cfg.banner()
    pinmap = load_pinmap(cfg)
    inputs, lasers, _adc_l, _adc_pt = claim_lineup_hardware(pinmap)
    mux = None
    try:
        mux = Mux4051(
            pinmap["adc_laser_gp"][0],
            pinmap["mux_a"],
            pinmap["mux_b"],
            pinmap["mux_c"],
        )
    except Exception as e:
        print("mux skip", e)
    fitted = pinmap.get("fitted") or [0]
    cap = _cap_duty(cfg)
    bells = []
    machines = []
    for i, ch in enumerate(fitted):
        y = None if mux is None else ch
        b = Bell(ch, inputs[i], lasers[i], mux=mux, mux_y=y)
        b.laser_on(cap)
        bells.append(b)
        t_ms = 2500
        try:
            t_ms = int(cfg.get_int("detection.swing_ms", 2500))
        except Exception:
            pass
        machines.append(BellMachine(str(ch + 1), t_ms=t_ms))
    sh, sb = _stand_flags(pinmap)
    now = time.monotonic() * 1000
    for b, m in zip(bells, machines):
        st = m.boot(int(now), b.pt(), sh, sb)
        print("boot bell", m.name, st)
    farm_log.init("bell_log_detect.txt")
    return bells, machines


async def scan_loop(bells, machines):
    while True:
        now = int(time.monotonic() * 1000)
        sh, sb = False, False
        for b, m in zip(bells, machines):
            st, ev = m.update(now, b.pt(), sh, sb, pattern=None)
            if ev:
                line = "bell %s %s %s" % (m.name, st, ev)
                print(line)
                farm_log.write(line)
        await asyncio.sleep(0.01)


async def main():
    bells, machines = setup()
    try:
        await scan_loop(bells, machines)
    finally:
        for b in bells:
            try:
                b.laser_on(0)
            except Exception:
                pass
        farm_log.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")
