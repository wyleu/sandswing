# loopback_test.py -- per-channel loopback self-test (boot / daily, tower
# inactive only).  Loopback plug, per channel:
#   path A  Green (laser PWM GPn)          -> Blue/White (sense GP0..)
#   path B  Green/White (inward 4051 Yn)   <- Orange (outward 4051 Yn)
# Path B: GP21 PWM -> RC -> outward COMMON (GP28) -> Yn -> Orange -> plug
#   -> Green/White -> inward Yn -> inward COMMON (100 k to GND) -> GP26
# Every step holds tower.select(bell) (shared-channel invariant).
# TODO(spec C3/C4): fixed sleeps below (0.01 s, 0.137 ms) -> derived waits.
from rc_timing import rc_settle_s
from adc_avg import avg as _avg
from signal_bands import (LB_RATIO_MIN, LB_LOAD_MIN, LB_LOAD_MAX,
                          UNLOADED_MIN, DAC_MAX_RESID, SWEEP_TOL)


def _fit(xs, ys):
    n = float(len(xs))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    g = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    o = my - g * mx
    return g, o, max(abs(y - (g * x + o)) for x, y in zip(xs, ys))


class LoopbackSelfTest:
    """Built on Tower/Bell.  Each bell needs .drive (DrivePath with GP28
    readback), .sense (SensePath on GP26); for path A also bell.laser
    (PWMOut) and bell.sense_pin (DigitalInOut input).  All steps hold the
    tower on that bell, so the shared-channel invariant is respected."""

    def __init__(self, tower, n_avg=16, sweep_samples=200):
        self.tower = tower
        self.n_avg = n_avg
        self.sweep_samples = sweep_samples

    def _sleep(self, s):
        self.tower.pins.sleep(s)

    def _thr(self, bell, duty):
        d = bell.drive
        v = d.verify
        d.verify = False                  # loaded nodes read low on purpose
        try:
            d.set(bell.y, duty)
        finally:
            d.verify = v
        # a plug changes the load: allow a full RC settle after selection
        self._sleep(rc_settle_s(d.tau_s, 0.2, d.settle_counts / 65535.0))
        return _avg(d.back, self.n_avg), _avg(bell.sense.adc, self.n_avg)

    def dac_linearity(self, bell, steps=8):
        """GP21 -> RC -> GP28.  Run on a channel WITHOUT a plug (unloaded)."""
        xs, ys = [], []
        with self.tower.select(bell):
            for k in range(steps + 1):
                duty = int(65535 * k / steps)
                r28, _ = self._thr(bell, duty)
                xs.append(duty)
                ys.append(r28)
            bell.drive.off()
        g, o, resid = _fit(xs, ys)
        ok = resid <= DAC_MAX_RESID and g > 0.9
        return {"test": "dac_linearity", "ch": bell.index, "ok": ok,
                "gain": g, "offset": o, "max_resid": resid}

    def threshold_loopback(self, bell, duty=32768):
        """Path B.  Returns plug=True/False and ok (consistent reading)."""
        with self.tower.select(bell):
            r28, r26 = self._thr(bell, duty)
            bell.drive.off()
        exp = bell.drive.expected(duty)
        load = r28 / float(exp) if exp else 0.0
        ratio = r26 / float(r28) if r28 else 0.0
        if ratio >= LB_RATIO_MIN:
            plug, ok, why = True, True, "looped"
        elif load >= UNLOADED_MIN and r26 < 0.25 * r28:
            plug, ok, why = False, True, "no plug (head or empty)"
        else:
            plug, ok, why = None, False, "inconsistent: check switch/cable/Ron"
        return {"test": "threshold_loopback", "ch": bell.index, "ok": ok,
                "plug": plug, "why": why, "gp28": r28, "gp26": r26,
                "load": load, "ratio": ratio}

    def digital_loopback(self, bell, settle=0.01):
        """Path A on/off: laser duty 0 -> sense False, 65535 -> True."""
        got = []
        with self.tower.select(bell):
            for duty in (0, 65535, 0):
                bell.laser.duty_cycle = duty
                self._sleep(settle)
                got.append(bool(bell.sense_pin.value))
            bell.laser.duty_cycle = 0
        ok = got == [False, True, False]
        plug = (True if ok else None) if any(got) else False
        return {"test": "digital_loopback", "ch": bell.index, "ok": ok,
                "plug": plug, "seen": got}

    def duty_sweep(self, bell, duties=(0, 16384, 32768, 49152, 65535)):
        """Path A 'analog': fraction of samples high vs PWM duty.  Valid only
        if the connector-board filter corner is well above the 1 kHz PWM;
        with heavy 'Dot A' filtering expect a step instead (flagged)."""
        frac = []
        with self.tower.select(bell):
            for duty in duties:
                bell.laser.duty_cycle = duty
                self._sleep(0.01)
                hi = 0
                for _ in range(self.sweep_samples):
                    hi += 1 if bell.sense_pin.value else 0
                    self._sleep(0.000137)   # not a multiple of the PWM period
                frac.append(hi / float(self.sweep_samples))
            bell.laser.duty_cycle = 0
        err = max(abs(f - d / 65535.0) for f, d in zip(frac, duties))
        mono = all(frac[i] <= frac[i + 1] + 0.02 for i in range(len(frac) - 1))
        return {"test": "duty_sweep", "ch": bell.index, "ok": err <= SWEEP_TOL,
                "monotonic": mono, "frac": frac, "max_err": err}

    def run(self, bells, dac_bell=None):
        """Boot / daily self-check.  Returns {"ok": bool, "channels": {...}}."""
        report = {"channels": {}}
        for b in bells:
            r = {"threshold": self.threshold_loopback(b)}
            if getattr(b, "laser", None) is not None and \
                    getattr(b, "sense_pin", None) is not None and r["threshold"]["plug"]:
                r["digital"] = self.digital_loopback(b)
                r["sweep"] = self.duty_sweep(b)
            report["channels"][b.index] = r
        free = [b for b in bells if report["channels"][b.index]["threshold"]["plug"] is False]
        db = dac_bell or (free[0] if free else None)
        report["dac"] = self.dac_linearity(db) if db is not None else \
            {"test": "dac_linearity", "ok": None, "why": "no unloaded channel"}
        oks = [t["ok"] for r in report["channels"].values() for t in r.values()]
        report["ok"] = all(oks) and report["dac"]["ok"] is not False
        return report
