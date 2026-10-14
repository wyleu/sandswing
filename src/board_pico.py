"""
board_pico.py
=============
Loopback + mux exercise for the Pico board.

Digital: sense ↔ laser_pwm (fails without loopback board — expected)
Current-sense mux (adc_mux): select Y0–Y7, read common
Threshold mux (threshold_mux): select Y0–Y7, read common
  (threshold voltage generation is a placeholder until the filter is fitted)

NeoPixel GP16:
  pixel 0–7 = channel under test
  pixel 6   = overall result
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
ADC_MUX = pinmap.get("adc_mux") or {}
THR_MUX = pinmap.get("threshold_mux") or {}

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

def claim_mux(mux):
    """Return (common AnalogIn, a, b, c DigitalInOut) or None."""
    if not mux or "common" not in mux:
        return None
    try:
        common = analogio.AnalogIn(gp(mux["common"]))
        a = digitalio.DigitalInOut(gp(mux["a"]))
        b = digitalio.DigitalInOut(gp(mux["b"]))
        c = digitalio.DigitalInOut(gp(mux["c"]))
        for p in (a, b, c):
            p.direction = digitalio.Direction.OUTPUT
        return common, a, b, c
    except Exception as e:
        print("  mux claim failed:", e)
        return None

def mux_select(a, b, c, y):
    a.value = bool(y & 1)
    b.value = bool(y & 2)
    c.value = bool(y & 4)

def test_digital():
    print("Digital loopback")
    ok = True
    for i, (in_n, out_n) in enumerate(zip(SENSE, LASER)):
        neo_clear()
        neo[i % 8] = (0, 0, 64)
        neo.show()
        try:
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
        except Exception as e:
            print("  FAIL GP%d/GP%d:" % (out_n, in_n), e)
            ok = False
    return ok

def test_mux(name, mux):
    print("%s mux" % name)
    claimed = claim_mux(mux)
    if not claimed:
        print("  not configured or claim failed")
        return False
    common, a, b, c = claimed
    ok = True
    ys = mux.get("laser_y") or mux.get("y") or list(range(8))
    for y in ys:
        neo_clear()
        neo[y % 8] = (64, 32, 0)
        neo.show()
        mux_select(a, b, c, y)
        time.sleep(0.01)
        try:
            v = common.value
            print("  Y%d raw=%d" % (y, v))
        except Exception as e:
            print("  Y%d read failed:" % y, e)
            ok = False
    common.deinit()
    a.deinit()
    b.deinit()
    c.deinit()
    return ok

print("board_pico test")
print("PINS sense", SENSE, "laser", LASER, "neo", NEO_PIN)
neo_clear()
d_ok = test_digital()
cs_ok = test_mux("current-sense", ADC_MUX)
th_ok = test_mux("threshold", THR_MUX)
all_ok = d_ok and cs_ok and th_ok
neo_clear()
neo_status(all_ok)
print("PASS" if all_ok else "FAIL")