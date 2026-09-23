"""
toll_detect.py
==============
Funeral / slow-toll stroke counter. One optical head per fitted wheel.

Filename: toll_detect.py
Runs on:  CircuitPython Pico 2 W via settings.json startup.program
          or:  exec(open("toll_detect.py").read())

NOT bell_detect.py. That file keeps the mux / Bell / asyncio / stand-pin
skeleton for the long-term tower program.

WHAT THIS IS FOR
    Two heads (pins.fitted, default [0, 1]) — typically tenor + one other.
    Park each laser at wheel.laser_duty. Read the matching PT (active-low).
    A wheel rising edge that BellMachine accepts as "started" is one STROKE.

    English whole pull = hand + back = 2 strokes.
        tell  child=3 / woman=6 / man=9  whole pulls
        then  N years as whole pulls
        then  the same tell again
        gaps: stand on the tape, wait for the GPS minute, pull off.
        Held-on-balance == STOOD to the optics. This program does not
        name a third state. Without a stand-tape head, long gaps look
        like IDLE; count bursts of strokes instead.

DOES
    load_config / load_pinmap / claim_lineup_hardware
    one BellMachine per fitted channel
    print one serial line per started / fault / lost
    optional farm_log if the module exists (safe if it does not)

DOES NOT
    Write logs as a requirement — capture serial on the Pi with tee
    MIDI, WebSocket, lineup sweep, 2-1-4 decode, stand GPIOs
    Guess age or tell

SETTINGS wheel block
    t_ms, lost_mult, stand_hold_ms
    optional laser_duty 0..65535  (default 32767)
"""

import time
import digitalio

from config_loader import load_config
from pins_from_settings import load_pinmap, claim_lineup_hardware
from bell_machine import BellMachine

try:
    import farm_log
except ImportError:
    farm_log = None


def _now_ms():
    return int(time.monotonic() * 1000)


def _pt(pin):
    return not pin.value


def _cfg_dict(cfg):
    if isinstance(cfg, dict):
        return cfg
    for name in ("_data", "_cfg", "data"):
        raw = getattr(cfg, name, None)
        if isinstance(raw, dict):
            return raw
    try:
        return cfg.as_dict()
    except Exception:
        return {}


def main():
    cfg = load_config()
    try:
        cfg.banner()
    except Exception:
        pass

    pinmap = load_pinmap(cfg)
    raw = _cfg_dict(cfg)
    wheel_cfg = raw.get("wheel", {}) if isinstance(raw, dict) else {}
    t_ms = int(wheel_cfg.get("t_ms", 2500))
    lost_mult = float(wheel_cfg.get("lost_mult", 1.5))
    hold_ms = int(wheel_cfg.get("stand_hold_ms", 350))
    duty = max(0, min(65535, int(wheel_cfg.get("laser_duty", 32767))))

    fitted = list(pinmap.get("fitted") or [0, 1])
    inputs, lasers, _adc_l, _adc_pt = claim_lineup_hardware(pinmap)

    names = ["tenor", "other"]
    machines = []
    counts = []
    last_stood = []

    for i, ch in enumerate(fitted):
        if ch >= len(inputs) or ch >= len(lasers):
            continue
        name = names[i] if i < len(names) else "ch%d" % (ch + 1)
        bm = BellMachine(name, t_ms=t_ms, lost_mult=lost_mult, stand_hold_ms=hold_ms)
        pin = inputs[ch]
        try:
            pin.pull = digitalio.Pull.UP
        except Exception:
            pass
        bm.boot(_now_ms(), _pt(pin), False, False)
        lasers[ch].duty_cycle = duty
        machines.append((ch, name, bm, pin))
        counts.append(0)
        last_stood.append(None)
        print("TOLL ch", ch + 1, name, "duty", duty, "boot", bm.state)

    if farm_log:
        try:
            farm_log.open_log("bell_log_detect.txt")
        except Exception:
            pass

    print("serial: one line per started/fault/lost — capture on the Pi")
    print("Ctrl-C to stop")

    try:
        while True:
            now = _now_ms()
            for i, (ch, name, bm, pin) in enumerate(machines):
                st, ev = bm.update(now, _pt(pin), False, False, pattern=None)
                if "stood" in ev:
                    last_stood[i] = now
                if not ev:
                    continue
                if "started" in ev:
                    counts[i] += 1
                    stood_ms = 0
                    if last_stood[i] is not None:
                        stood_ms = now - last_stood[i]
                        last_stood[i] = None
                    line = "%d ch%d %s n=%d stood_ms=%d %s %s" % (
                        now, ch + 1, name, counts[i], stood_ms, st, ",".join(ev)
                    )
                else:
                    line = "%d ch%d %s n=%d %s %s" % (
                        now, ch + 1, name, counts[i], st, ",".join(ev)
                    )
                print(line)
                if farm_log:
                    try:
                        farm_log.write(line)
                    except Exception:
                        pass
            time.sleep(0.005)
    finally:
        for ch, name, bm, pin in machines:
            try:
                lasers[ch].duty_cycle = 0
            except Exception:
                pass
        if farm_log:
            try:
                farm_log.close()
            except Exception:
                pass
        print("TOLL stop", list(zip([n for _, n, _, _ in machines], counts)))


if __name__ == "__main__":
    main()