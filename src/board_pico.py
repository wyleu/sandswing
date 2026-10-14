"""
board_pico.py
=============
Loopback + mux exercise + threshold ramp linearity check.

Digital: sense ↔ laser_pwm (fails without loopback — expected)
Current-sense mux: walk Y0–Y7
Threshold mux: slow PWM ramp on the pin defined in settings
               (threshold_mux.pwm_gp, default 28), read back via mux common,
               display ramp on pixels 0–7, result on pixel 6.
"""

import time
import board
import digitalio
import analogio
import pwmio
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
THRESH_PWM_GP = (THR_MUX.get("pwm_gp") if THR_MUX else None) or 28

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

def test_threshold_loop():
    """Hold PWM at 50 %, walk threshold-mux Y0–Y3, read current-sense common."""
    print("Threshold loop (Y0–Y3 → Vsense)")
    thr = claim_mux(THR_MUX)
    cs  = claim_mux(ADC_MUX)
    if not thr or not cs:
        print("  mux not available")
        return False
    thr_common, ta, tb, tc = thr
    cs_common,  ca, cb, cc = cs
    try:
        pwm = pwmio.PWMOut(gp(THRESH_PWM_GP), frequency=1000, duty_cycle=32767)
    except Exception as e:
        print("  PWM claim failed:", e)
        return False

    ok = True
    for y in range(4):
        neo_clear()
        neo[y] = (0, 32, 32)
        neo.show()
        mux_select(ta, tb, tc, y)
        time.sleep(0.05)
        try:
            v = cs_common.value
            print("  Y%d vsense raw=%d" % (y, v))
        except Exception as e:
            print("  Y%d read failed:" % y, e)
            ok = False
    pwm.duty_cycle = 0
    pwm.deinit()
    for p in (thr_common, ta, tb, tc, cs_common, ca, cb, cc):
        p.deinit()
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

def test_threshold_ramp():
    """Slow PWM ramp on THRESH_PWM_GP, read back via threshold mux common."""
    print("Threshold ramp (PWM GP%d)" % THRESH_PWM_GP)
    claimed = claim_mux(THR_MUX)
    if not claimed:
        print("  threshold mux not available")
        return False
    common, a, b, c = claimed
    mux_select(a, b, c, 0)          # park on Y0 for the ramp
    try:
        pwm = pwmio.PWMOut(gp(THRESH_PWM_GP), frequency=1000, duty_cycle=0)
    except Exception as e:
        print("  PWM claim failed:", e)
        common.deinit(); a.deinit(); b.deinit(); c.deinit()
        return False

    steps = 16
    readings = []
    ok = True
    for s in range(steps + 1):
        duty = int(65535 * s / steps)
        pwm.duty_cycle = duty
        lit = int(8 * s / steps)
        neo_clear()
        for i in range(lit):
            neo[i] = (0, 32, 0)
        neo.show()
        time.sleep(0.15)
        try:
            v = common.value
            readings.append((duty, v))
            print("  duty=%5d raw=%5d" % (duty, v))
        except Exception as e:
            print("  read failed:", e)
            ok = False
    pwm.duty_cycle = 0
    pwm.deinit()
    common.deinit()
    a.deinit()
    b.deinit()
    c.deinit()

    if len(readings) > 2:
        rising = sum(1 for i in range(1, len(readings)) if readings[i][1] >= readings[i-1][1])
        print("  rising steps %d/%d" % (rising, len(readings) - 1))
        if rising < (len(readings) - 1) * 0.6:
            ok = False
    return ok

print("board_pico test")
print("PINS sense", SENSE, "laser", LASER, "neo", NEO_PIN)
neo_clear()
d_ok = test_digital()
cs_ok = test_mux("current-sense", ADC_MUX)
th_ok = test_threshold_ramp()
tt_ok = test_threshold_loop()

all_ok = d_ok and cs_ok and th_ok and tt_ok

neo_clear()
neo_status(all_ok)
print("PASS" if all_ok else "FAIL")