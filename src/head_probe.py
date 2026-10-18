# head_probe.py
import time
import board
import digitalio
import pwmio
import analogio

PWM_FREQ = 1000
PULSE = 32768
SETTLE_S = 0.05
CLASS_S = 0.25
MID = 32768

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

def _laser_present(laser, adc, present_delta):
    laser.duty_cycle = 0
    time.sleep(SETTLE_S)
    dark = adc.value
    laser.duty_cycle = PULSE
    time.sleep(SETTLE_S)
    lit = adc.value
    laser.duty_cycle = 0
    return dark, lit, (lit - dark) >= present_delta

def _deltas(cfg):
    p = (cfg or {}).get("probe", {})
    return int(p.get("present_delta", 2000)), int(p.get("lb_delta", 1000))

def _classify_held(sense_pin, laser):
    laser.duty_cycle = 0
    time.sleep(CLASS_S)
    low = not sense_pin.value
    print("  sense low=%s" % low)
    return "old" if low else "new"

def _thr_loopback(thr_gp, adc):
    thr = pwmio.PWMOut(_gp(thr_gp), frequency=PWM_FREQ, duty_cycle=MID)
    try:
        time.sleep(SETTLE_S)
        dark = adc.value
        thr.duty_cycle = MID
        time.sleep(SETTLE_S)
        lit = adc.value
        return dark, lit, lit - dark
    finally:
        _release(thr)

def scan_heads(pinmap, cfg, sense_pins):
    """Probe every channel. sense_pins already claimed by concentrator.
    Returns 'empty'/'old'/'new'/'lb'.

    NOTE: threshold_mux.pwm_gp (GP28) shares PWM slice 6A with laser GP12
    (channel 5). Do not hold thr_pwm while claiming a laser.
    Long-term: move threshold PWM to GP21/GP22. For now thr_pwm is
    created only for the loopback test, after the laser is released.
    """
    
    present_delta, lb_delta = _deltas(cfg)
    sense_gps = list(pinmap.get("sense_gp") or [])
    laser_gps = list(pinmap.get("laser_gp") or [])
    n = min(len(sense_gps), len(laser_gps), len(sense_pins))
    mux = (cfg or {}).get("pins", {}).get("adc_mux", {})
    thr = (cfg or {}).get("pins", {}).get("threshold_mux", {})
    ys = mux.get("laser_y") or list(range(8))
    thr_gp = thr.get("pwm_gp", 28)
    results = ["empty"] * n
    addr = adc = None
    try:
        addr = _claim_addr(mux)
        if addr is None:
            return results
        a, b, c = addr
        adc = analogio.AnalogIn(_gp(mux.get("common", 26)))
        for i in range(n):
            y = ys[i] if i < len(ys) else i
            laser = None
            try:
                _select(a, b, c, y)
#                 td, tl, rise = _thr_loopback(thr_gp, adc)
#                 print("ch %d thr dark=%d lit=%d rise=%d" % (i + 1, td, tl, rise))
#                 if rise >= lb_delta:
#                     results[i] = "lb"
#                     continue
                print("ch %d laser GP%d" % (i + 1, laser_gps[i]))
                laser = pwmio.PWMOut(_gp(laser_gps[i]), frequency=PWM_FREQ, duty_cycle=0)
                dark, lit, present = _laser_present(laser, adc, present_delta)
                print("ch %d dark=%d lit=%d" % (i + 1, dark, lit))
                if present:
                    results[i] = _classify_held(sense_pins[i], laser)
            except Exception as e:
                print("ch %d skip: %s" % (i + 1, e))
                results[i] = "empty"
            finally:
                _release(laser)
                time.sleep(0.02)
    finally:
        _release(adc)
        if addr:
            _release(*addr)
    return results