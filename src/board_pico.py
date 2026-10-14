"""
board_pico.py
=============
Standalone loopback test for the Pico + 4051 board.

Uses pins_from_settings.py so all GP numbers come from settings.json.
No arithmetic, no hard-coded bases.

Digital loopback (36-way connector):
  sense GP → laser_pwm GP   (or the pairs you define in settings)

Analogue loopback:
  first 4 ADC channels (adc_laser + adc_pt as available)

NeoPixel (neopixel_gp) shows progress; pixel 6 is overall status.
"""

import time
import board
import digitalio
import analogio
import neopixel

from config_loader import load_config
from pins_from_settings import load_pinmap

cfg = load_config()
pinmap = load_pinmap(cfg)

NEO_PIN = pinmap["neopixel_gp"]
SENSE   = pinmap["sense_gp"]
LASER   = pinmap["laser_gp"]
ADC_LASER = pinmap.get("adc_laser_gp") or []
ADC_PT    = pinmap.get("adc_pt_gp")

NUM_PIXELS = 8
neo = neopixel.NeoPixel(getattr(board, "GP%d" % NEO_PIN), NUM_PIXELS,
                        brightness=0.3, auto_write=False)

def neo_clear():
    for i in range(NUM_PIXELS):
        neo[i] = (0, 0, 0)
    neo.show()

def neo_status(ok):
    neo[6] = (0, 64, 0) if ok else (64, 0, 0)
    neo.show()

def gp(n):
    return getattr(board, "GP%d" % int(n))

def test_digital():
    print("Digital loopback (sense ↔ laser_pwm)")
    ok = True
    pairs = list(zip(SENSE, LASER))
    for i, (in_n, out_n) in enumerate(pairs):
        neo_clear()
        neo[i % 8] = (0, 0, 64)
        neo.show()
        out = digitalio.DigitalInOut(gp(out_n))
        out.direction = digitalio.Direction.OUTPUT
        inp = digitalio.DigitalInOut(gp(in_n))
        inp.direction = digitalio.Direction.INPUT
        inp.pull = digitalio.Pull.DOWN
        for val in (True, False):
            out.value = val
            time.sleep(0.01)
            if inp.value != val:
                print("  FAIL out GP%d in GP%d expected %s" % (out_n, in_n, val))
                ok = False
        out.deinit()
        inp.deinit()
    return ok

def test_analog():
    print("Analogue presence (first 4)")
    ok = True
    adcs = list(ADC_LASER)
    if ADC_PT is not None:
        adcs.append(ADC_PT)
    for i, n in enumerate(adcs[:4]):
        neo_clear()
        neo[i] = (64, 32, 0)
        neo.show()
        try:
            ain = analogio.AnalogIn(gp(n))
            v = ain.value
            ain.deinit()
            print("  ADC GP%d raw=%d" % (n, v))
        except Exception as e:
            print("  ADC GP%d error:" % n, e)
            ok = False
    return ok

print("board_pico test")
print("PINS sense", SENSE, "laser", LASER, "neo", NEO_PIN)
neo_clear()
d_ok = test_digital()
a_ok = test_analog()
all_ok = d_ok and a_ok
neo_status(all_ok)
print("PASS" if all_ok else "FAIL")