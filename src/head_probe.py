# head_probe.py
import time
import board
import digitalio
import pwmio
import analogio

PWM_FREQ = 1000
PULSE = 32768
MID = 32768
SETTLE_S = 0.05
CLASS_S = 0.02
PRESENT_DELTA = 5000
LB_DELTA = 10000

def _gp(n):
    return getattr(board, "GP%d" % int(n))

def _release(*objs):
    for o in objs:
        if o is None:
            continue
        try:
            if hasattr(o, "duty_cycle"):
                o.duty_cycle = 0
            o.deinit()
        except Exception:
            pass

def _claim_addr(mux):
    if not mux or mux.get("a") is None:
        return None
    a = digitalio.DigitalInOut(_gp(mux["a"]))
    b = digitalio.DigitalInOut(_gp(mux["b"]))
    c = digitalio.DigitalInOut(_gp(mux["c"]))
    for p in (a, b, c):
        p.direction = digitalio.Direction.OUTPUT
        p.value = False
    return a, b, c

def _select(a, b, c, y):
    y = int(y) & 7
    a.value = bool(y & 1)
    b.value = bool(y & 2)
    c.value = bool(y & 4)

def scan_heads(pinmap, cfg, sense_pins):
    """Probe every channel. sense_pins is the list already claimed by concentrator.
    Returns a list of 'empty'/'old'/'new'/'lb'.
    """
    sense_gps = list(pinmap.get("sense_gp") or [])
    laser_gps = list(pinmap.get("laser_gp") or [])
    n = min(len(sense_gps), len(laser_gps), len(sense_pins))
    mux = (cfg or {}).get("pins", {}).get("adc_mux", {})
    thr = (cfg or {}).get("pins", {}).get("threshold_mux", {})
    ys = mux.get("laser_y") or list(range(8))
    results = ["empty"] * n
    addr = adc = thr_pwm = None
    try:
        addr = _claim_addr(mux)
        if addr is None:
            return results
        a, b, c = addr
        adc = analogio.AnalogIn(_gp(mux.get("common", 26)))
        thr_pwm = pwmio.PWMOut(_gp(thr.get("pwm_gp", 28)), frequency=PWM_FREQ, duty_cycle=0)
        for i in range(n):
            y = ys[i] if i < len(ys) else i
            laser = None
            try:
                try:
                    laser = pwmio.PWMOut(_gp(laser_gps[i]), frequency=PWM_FREQ, duty_cycle=0)
                except Exception as e:
                    print("ch %d laser skip: %s" % (i + 1, e))
                    results[i] = "empty"
                    continue
                _select(a, b, c, y)
                thr_pwm.duty_cycle = 0
                time.sleep(SETTLE_S)
                dark = adc.value
                laser.duty_cycle = PULSE
                time.sleep(SETTLE_S)
                lit = adc.value
                laser.duty_cycle = 0
                present = (lit - dark) >= PRESENT_DELTA
                print("ch %d dark=%d lit=%d" % (i + 1, dark, lit))
                if present:
                    time.sleep(CLASS_S)
                    held = not sense_pins[i].value
                    results[i] = "old" if held else "new"
                    continue
                thr_pwm.duty_cycle = MID
                time.sleep(SETTLE_S)
                v = adc.value
                thr_pwm.duty_cycle = 0
                time.sleep(SETTLE_S)
                print("ch %d thr=%d rise=%d" % (i + 1, v, v - dark))
                if (v - dark) >= LB_DELTA:
                    results[i] = "lb"
            finally:
                _release(laser)
                time.sleep(0.02)
    finally:
        _release(thr_pwm, adc)
        if addr:
            _release(*addr)
    return results