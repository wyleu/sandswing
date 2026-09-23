"""
sandswing.py
============
Optical lineup + optional WS rounds jabber. Pico 2 W / CircuitPython 10.0.3.

START
    CIRCUITPY runs code.py → settings.json "startup.program" → sandswing.py
    Master: ~/Code/Sandbells/sandswing/src/sandswing.py
    Stamp only on CIRCUITPY. Do not remount the USB disk in boot.py.

DOES
    Sweep one laser at a time 0–100% PWM.
    Digital PT on sense[] (pull-up; PT True/False from Farm.pt()).
    Analogue I via CD4051 (ADC0 GP26, ABC GP17/18/19), Farm.analog().
    NeoPixel GP16: pixel 0 status, pixel ch+1 under test.
    Optional WS jabber (does not drive lasers).

DOES NOT
    Real blow detection, MIDI, create_hardware(), ir_pwm/tx/status LEDs,
    writing CIRCUITPY while the Pi has the volume mounted (log → serial).

LOOM (workshop 2026-09-23)
    sense      [0, 1, 2, 3]     ch0/ch1 fitted
    laser_pwm  [8, 9, 10, 11]   1 kHz PWM
    neopixel   16
    adc_mux    common=26  A=17 B=18 C=19
               laser_y [0..7] → 4051 Y0..Y7 = laser current for ch 0..7
    fitted     [0, 1]

    Serial must show:
      --- ch 1 laser GP8 PT GP0 ---
      --- ch 2 laser GP9 PT GP1 ---

REQUIRED ON CIRCUITPY
    boot.py code.py sandswing.py pins_from_settings.py
    bell.py mux4051.py sand_status.py
    config_loader.py farm_log.py farm_ws.py settings.json lib/


settings.json PINS
    "pins": {
      "sense": [0, 1, 2, 3],
      "laser_pwm": [7, 8, 9, 10],
      "neopixel": 16,
      "adc_laser": [26, 27],
      "adc_pt": 28,
      "fitted": [0, 1]
    }
    "detection": { "num_bells": 4 }
    "output.enabled_formats" must include "neopixel" or the strip stays dark.

BOOT CHECK
    PINS sense [0, 1, 2, 3] laser [7, 8, 9, 10] neo 16 fitted [0, 1]
    --- ch 1 laser GP7 PT GP0 ---

MASTER COPY
    ~/Code/Sandbells/sandswing/src/sandswing.py
    GitHub repo wyleu/sandswing. CIRCUITPY is a stamp only.
"""

import asyncio
import time
import math
import board
import wifi
import farm_log

from config_loader import load_config
from farm_ws import connect_wifi, start_server, poll, send
from pins_from_settings import load_pinmap

from sand_status import from_sandswing
from bell import Farm

COLOR_A = (0, 64, 0)
COLOR_B = (64, 0, 0)
STATUS = (0, 32, 0)

cfg = load_config()
cfg.banner()

pinmap = load_pinmap(cfg)

farm = Farm.from_pinmap(pinmap)

SENSE_GPS = pinmap.get("sense_gp") or pinmap.get("sense")
LASER_GPS = pinmap.get("laser_gp") or pinmap.get("laser_pwm")

FITTED = set(pinmap["fitted"])
NUM = min(len(SENSE_GPS), len(LASER_GPS), cfg.get_int("detection.num_bells", 4))


SWEEP_STEPS = cfg.get_int("sensing.test.sweep_steps", 20)
SETTLE_S = cfg.get_float("sensing.test.settle_s", 0.1)
REPEATS = cfg.get_int("sensing.test.sweep_repeats", 2)
JABBER = cfg.get_bool("streams.bell.test_sweep", False)
ROUNDS_N = cfg.get_int("streams.bell.rounds_bells", 8)
BLOW_S = cfg.get_float("streams.bell.blow_s", 0.32)
GAP_S = cfg.get_float("streams.bell.gap_s", 0.32)
PERIOD = ROUNDS_N * BLOW_S + GAP_S
neo_phase = [False] * NUM
_last_line = ""
_ch = 0
_duty = 0
_pt = None
_changes = 0
_pass = None


neo = None
status_color = STATUS
if cfg.format_enabled("neopixel"):
    try:
        import neopixel

        neo_conf = cfg.output.get("neopixel") or {}
        pin_n = int(pinmap["neopixel_gp"] or neo_conf.get("pin", 16))
        bright = float(neo_conf.get("brightness", 0.25))
        order = getattr(
            neopixel,
            str(neo_conf.get("pixel_order", "GRB")).upper(),
            neopixel.GRB,
        )
        status_color = tuple(neo_conf.get("status_color", list(STATUS)))
        neo = neopixel.NeoPixel(
            getattr(board, "GP%d" % pin_n),
            NUM + 1,
            brightness=bright,
            auto_write=False,
            pixel_order=order,
        )
        neo[0] = status_color
        for i in range(1, len(neo)):
            neo[i] = (0, 0, 0)
        neo.show()
        print("neopixel GP%d" % pin_n)
    except Exception as e:
        print("neopixel failed:", e)
        neo = None


def neo_select(ch):
    if not neo:
        return
    neo[0] = status_color
    for i in range(1, len(neo)):
        neo[i] = (16, 16, 16) if (i == ch + 1) else (0, 0, 0)
    neo.show()


def neo_toggle_channel(ch):
    if not neo:
        return
    neo_phase[ch] = not neo_phase[ch]
    neo[0] = status_color
    neo[ch + 1] = COLOR_B if neo_phase[ch] else COLOR_A
    neo.show()


farm_log.start(cfg)
print("Optical lineup", "ch", NUM, "steps", SWEEP_STEPS, "fitted", sorted(FITTED))


def emit(msg):
    global _last_line
    _last_line = str(msg)
    print(msg)
    farm_log.write(msg)

def status_payload():
    ip = None
    ssid = None
    try:
        ip = str(wifi.radio.ipv4_address)
    except Exception:
        pass
    try:
        if wifi.radio.ap_info:
            ssid = wifi.radio.ap_info.ssid
    except Exception:
        pass
    phase = False
    if _ch is not None and 0 <= _ch < len(neo_phase):
        phase = neo_phase[_ch]
    try:
        ts = int(time.time())
    except Exception:
        ts = 0
    return from_sandswing({
        "id": cfg.device_name,
        "name": cfg.device_name,
        "family": getattr(cfg, "family", "sandswing"),
        "role": getattr(cfg, "role", "swing"),
        "location": getattr(cfg, "location", ""),
        "ip": ip,
        "ssid": ssid,
        "mode": "rounds_jabber" if JABBER else "lineup",
        "channel": None if _ch is None else _ch + 1,
        "laser_gp": LASER_GPS[_ch] if _ch is not None else None,
        "sense_gp": SENSE_GPS[_ch] if _ch is not None else None,
        "duty": _duty,
        "duty_pct": int(100.0 * _duty / 65535.0) if _duty is not None else None,
        "pt": _pt,
        "changes": _changes,
        "pass_totals": _pass,
        "fitted": sorted(FITTED),
        "neo_on": bool(neo),
        "neo_phase": phase,
        "last_line": _last_line,
        "ts": ts,
        "poll_hint_sec": 2,
    })


def emit_tape(bell, u, amp=1.0, quiet=False):
    if not cfg.get_bool("streams.bell.enabled", False):
        return
    send({
        "type": "bell",
        "bell": int(bell),
        "u": float(u),
        "amp": float(amp),
        "t": time.monotonic(),
        "test": True if JABBER else False,
    })
    if not quiet:
        emit("WS bell=%d u=%.2f" % (bell, u))


async def rounds_jabber_task():
    last_u = [None] * (ROUNDS_N + 1)
    emit("WS ROUNDS 1..%d blow=%.2f gap=%.2f" % (ROUNDS_N, BLOW_S, GAP_S))
    while True:
        phase = time.monotonic() % PERIOD
        for b in range(1, ROUNDS_N + 1):
            local = phase - (b - 1) * BLOW_S
            if 0.0 <= local < BLOW_S:
                u = 2.5 + 2.4 * math.sin((local / BLOW_S) * math.pi)
            else:
                u = 2.5
            if last_u[b] is None or abs(u - last_u[b]) > 0.08:
                emit_tape(b, u, 1.0 if u > 2.6 else 0.0, quiet=True)
                last_u[b] = u
        poll()
        await asyncio.sleep(0.04)


def setup_net():
    if not connect_wifi(cfg):
        emit("No WiFi – lineup still runs")
        return
    start_server(cfg, status_payload)
    emit("WS ROUNDS jabber 1..%d" % ROUNDS_N if JABBER else "WS live")


async def ws_poll_task():
    while True:
        poll()
        await asyncio.sleep(0.05)


async def test_channel(bell):
    ch = bell.index
    emit(
        "--- ch %d laser GP%d PT GP%d %s---"
        % (
            ch + 1,
            LASER_GPS[ch],
            SENSE_GPS[ch],
            "" if ch in FITTED else "(no head) ",
        )
    )
    global _ch, _duty, _pt, _changes
    _ch = ch
    
    neo_select(ch)
    last = bell.pt()
    changes = 0
    curve = []
    for step in range(SWEEP_STEPS + 1):
        duty = int(65535 * step / SWEEP_STEPS)
        for _ in range(REPEATS):
            bell.laser(duty)
            await asyncio.sleep(SETTLE_S)
            raw = bell.pt()
            _duty = duty
            _pt = raw
            _changes = changes
            

            i_sense = bell.analog()
            emit(
                "ch %d duty=%5d (%3.0f%%) PT=%s I=%s"
                % (
                    ch + 1,
                    duty,
                    100.0 * duty / 65535.0,
                    raw,
                    i_sense if i_sense is not None else "n/a",
                )
            )
            if raw != last:
                last = raw
                changes += 1
                neo_toggle_channel(ch)
                emit("ch %d THRESHOLD duty=%d PT→%s" % (ch + 1, duty, raw))
            curve.append((duty, 1 if raw else 0))
    bell.laser_off()
    active = [d for d, s in curve if s]
    if active:
        emit("ch %d first_PT_True~%d changes=%d" % (ch + 1, active[0], changes))
    else:
        emit("ch %d never PT_True changes=%d" % (ch + 1, changes))
    return changes


async def lineup_loop():
    global _pass
    farm.all_lasers_off()
    emit("LINEUP START %s %s" % (cfg.family, cfg.device_name))
    while True:
        totals = []
        for bell in farm.fitted():
            totals.append(await test_channel(bell))
        _pass = totals
        emit("PASS %s" % (totals,))


async def main():
    setup_net()
    tasks = [lineup_loop(), ws_poll_task()]
    if JABBER:
        tasks.append(rounds_jabber_task())
    await asyncio.gather(*tasks)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped")
finally:
    farm.deinit()
    if neo:
        for i in range(len(neo)):
            neo[i] = (0, 0, 0)
        neo.show()
    farm_log.close()
    print("Cleanup complete")
