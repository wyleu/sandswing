"""
sandswing.py
============
CircuitPython optical lineup + optional fake WebSocket "rounds" jabber
for Raspberry Pi Pico 2 W.

HOW IT IS STARTED
    CircuitPython always runs code.py.
    code.py is the settings launcher. settings.json has:
        "startup": { "program": "sandswing.py" }
    The launcher should __import__("sandswing"), not exec().
    This file must be named sandswing.py on CIRCUITPY (not code.py).

WHAT THIS PROGRAM DOES
    1. load_config(), farm_log, optional Wi-Fi + WebSocket.
    2. Pins from settings.json via pins_from_settings.py (no input_base).
    3. Sweep each laser PWM 0% → 100% while reading the matching
       photodiode (active-low). Log duty, PT, optional ADC, edges.
    4. NeoPixel GP16: pixel 0 = status, pixel n+1 = channel under test.
    5. Optional synthetic rounds on WS if streams.bell.test_sweep
       AND streams.bell.enabled. Does not fire lasers or sensors.

WHAT THIS PROGRAM DOES NOT DO
    - Real bell-blow detection / mapping WS bells 1–8 onto GP0–3
    - Several lasers on at once
    - ir_pwm / tx_led / status_led (old sandbells pins)
    - create_hardware() (that double-claimed GP0/GP7)
    - MIDI
    - Writing the CIRCUITPY volume label
    - Saving calibration back into settings.json

THIS LOOM (2026-09-20)
    Sense (pull-up, PT = not pin.value):
        ch0 GP0 HEAD    ch1 GP1 HEAD    ch2 GP2 empty    ch3 GP3 empty
    Laser PWM 1 kHz:
        ch0 GP7         ch1 GP8         ch2 GP9          ch3 GP10
    If the "input 2" laser lights during software ch 4, that lead is on
    GP10 not GP8 — put "laser_pwm": [7, 10, 9, 8] until the loom is fixed.
    NeoPixel GP16. ADC laser GP26/27 (ch0/ch1 only). ADC PT GP28.

    Expect PASS [n, m, 0, 0] when both fitted heads see their beam.

REQUIRED ON CIRCUITPY
    code.py  sandswing.py  pins_from_settings.py
    config_loader.py  farm_log.py  farm_ws.py
    settings.json  lib/  boot.py (pause before code.py)

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
from pins_from_settings import load_pinmap, claim_lineup_hardware

COLOR_A = (0, 64, 0)
COLOR_B = (64, 0, 0)
STATUS = (0, 32, 0)

cfg = load_config()
cfg.banner()

pinmap = load_pinmap(cfg)
SENSE_GPS = pinmap["sense_gp"]
LASER_GPS = pinmap["laser_gp"]
FITTED = set(pinmap["fitted"])
NUM = min(len(SENSE_GPS), len(LASER_GPS), cfg.get_int("detection.num_bells", 4))

inputs, laser_pwms, adc_laser, adc_pt = claim_lineup_hardware(pinmap)

SWEEP_STEPS = cfg.get_int("sensing.test.sweep_steps", 20)
SETTLE_S = cfg.get_float("sensing.test.settle_s", 0.1)
REPEATS = cfg.get_int("sensing.test.sweep_repeats", 2)
JABBER = cfg.get_bool("streams.bell.test_sweep", False)
ROUNDS_N = cfg.get_int("streams.bell.rounds_bells", 8)
BLOW_S = cfg.get_float("streams.bell.blow_s", 0.32)
GAP_S = cfg.get_float("streams.bell.gap_s", 0.32)
PERIOD = ROUNDS_N * BLOW_S + GAP_S
neo_phase = [False] * NUM


def laser_duty(ch, duty):
    duty = max(0, min(65535, int(duty)))
    for o in range(NUM):
        laser_pwms[o].duty_cycle = duty if o == ch else 0


def lasers_all_off():
    for p in laser_pwms:
        p.duty_cycle = 0


def read_laser_current(ch):
    if not adc_laser or ch >= len(adc_laser):
        return None
    try:
        return adc_laser[ch].value
    except Exception:
        return None


def read_pt_v():
    if adc_pt is None:
        return None
    try:
        return adc_pt.value
    except Exception:
        return None


def read_pt(ch):
    return not inputs[ch].value


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


def emit(msg):
    print(msg)
    farm_log.write(msg)


farm_log.start(cfg)
print("Optical lineup", "ch", NUM, "steps", SWEEP_STEPS, "fitted", sorted(FITTED))


def status_payload():
    ip = None
    try:
        ip = str(wifi.radio.ipv4_address)
    except Exception:
        pass
    return {
        "name": cfg.device_name,
        "family": cfg.family,
        "role": getattr(cfg, "role", None),
        "ip": ip,
        "mode": "rounds_jabber" if JABBER else "lineup",
        "jabber": JABBER,
        "num": NUM,
        "fitted": sorted(FITTED),
        "ws": True,
    }


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


async def test_channel(ch):
    emit(
        "--- ch %d laser GP%d PT GP%d %s---"
        % (
            ch + 1,
            LASER_GPS[ch],
            SENSE_GPS[ch],
            "" if ch in FITTED else "(no head) ",
        )
    )
    neo_select(ch)
    last = read_pt(ch)
    changes = 0
    curve = []
    for step in range(SWEEP_STEPS + 1):
        duty = int(65535 * step / SWEEP_STEPS)
        for _ in range(REPEATS):
            laser_duty(ch, duty)
            await asyncio.sleep(SETTLE_S)
            raw = read_pt(ch)
            i_sense = read_laser_current(ch)
            pt_v = read_pt_v()
            emit(
                "ch %d duty=%5d (%3.0f%%) PT=%s PT_v=%s I=%s"
                % (
                    ch + 1,
                    duty,
                    100.0 * duty / 65535.0,
                    raw,
                    pt_v if pt_v is not None else "n/a",
                    i_sense if i_sense is not None else "n/a",
                )
            )
            if raw != last:
                last = raw
                changes += 1
                neo_toggle_channel(ch)
                emit("ch %d THRESHOLD duty=%d PT→%s" % (ch + 1, duty, raw))
            curve.append((duty, 1 if raw else 0))
    lasers_all_off()
    active = [d for d, s in curve if s]
    if active:
        emit("ch %d first_PT_True~%d changes=%d" % (ch + 1, active[0], changes))
    else:
        emit("ch %d never PT_True changes=%d" % (ch + 1, changes))
    return changes


async def lineup_loop():
    lasers_all_off()
    emit("LINEUP START %s %s" % (cfg.family, cfg.device_name))
    while True:
        totals = []
        for ch in range(NUM):
            totals.append(await test_channel(ch))
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
    lasers_all_off()
    if neo:
        for i in range(len(neo)):
            neo[i] = (0, 0, 0)
        neo.show()
    farm_log.close()
    print("Cleanup complete")
