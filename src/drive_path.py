# drive_path.py -- outward 4051: GP21 PWM -> 2-stage RC -> output 4051
# COMMON (pin 3) -> selected bell's Orange wire (LM393 threshold).  GP28
# also sits on the COMMON, so it reads the threshold back BEFORE the switch.
#
# The 1 uF is upstream of the mux: changing channel does not re-settle the
# RC; changing DUTY does (~170 ms for a full-scale step to 1/4096).
# pwm (GP21 PWMOut) and back (GP28 AnalogIn) are injected by the builder.
from mux_errors import NotSelected, ThresholdMismatch
from rc_timing import rc2_tau_s, rc_settle_s

DEFAULT_RC = (10e3, 100e-9, 10e3, 1e-6)


class DrivePath:
    def __init__(self, tower, pwm, back=None, rc=None, tol_counts=1024,
                 settle_counts=16, gain=1.0, offset=0, verify=True, sleep=None):
        self.tower = tower
        self.pwm = pwm
        self.back = back
        self._sleep = sleep or tower.pins.sleep
        r1, c1, r2, c2 = rc or DEFAULT_RC
        self.tau_s = rc2_tau_s(r1, c1, r2, c2)
        self.fast_s = 0.0005                     # fast pole (~0.49 ms) margin
        self.tol_counts = int(tol_counts)        # readback acceptance window
        self.settle_counts = int(settle_counts)  # 16 = 1 LSB of 12-bit
        self.gain, self.offset = float(gain), int(offset)
        self.verify = bool(verify) and back is not None
        self.duty = 0
        self.last_settle_s = 0.0

    def settle_for(self, new_duty):
        step = (int(new_duty) - self.duty) / 65535.0
        return rc_settle_s(self.tau_s, step, self.settle_counts / 65535.0,
                           self.fast_s if step else 0.0)

    def expected(self, duty):
        return int(self.offset + self.gain * int(duty))

    def _apply(self, y, duty):
        if not self.tower.is_held_by(y):
            raise NotSelected("drive ch %d without tower.select" % int(y))
        duty = max(0, min(65535, int(duty)))
        wait = self.settle_for(duty)
        self.pwm.duty_cycle = duty
        self.duty = duty
        self.last_settle_s = wait
        return duty, wait

    def _check(self, y, duty):
        if not self.verify:
            return None
        got = self.readback(y)
        exp = self.expected(duty)
        if abs(got - exp) > self.tol_counts:
            raise ThresholdMismatch("ch %d duty=%d expected~%d got %d (tol %d)"
                                    % (int(y), duty, exp, got, self.tol_counts))
        return got

    def set(self, y, duty, wait=True):
        """Set the threshold for the selected bell; wait the RC settle and
        verify on GP28.  Returns the readback (None if verify is off)."""
        duty, t = self._apply(y, duty)
        if not wait:
            return None
        if t:
            self._sleep(t)
        return self._check(y, duty)

    def readback(self, y):
        if self.back is None:
            return None
        if not self.tower.is_held_by(y):
            raise NotSelected("readback ch %d without tower.select" % int(y))
        _ = self.back.value
        return self.back.value

    def calibrate(self, y, lo=8192, hi=57344):
        """Two-point gain/offset fit of GP28 vs duty (selected bell held)."""
        v = self.verify
        self.verify = False
        try:
            self.set(y, lo)
            r_lo = self.readback(y)
            self.set(y, hi)
            r_hi = self.readback(y)
        finally:
            self.verify = v
        self.gain = (r_hi - r_lo) / float(hi - lo)
        self.offset = int(r_lo - self.gain * lo)
        return self.gain, self.offset

    def off(self):
        self.pwm.duty_cycle = 0
        self.duty = 0

    def status(self):
        return {"duty": self.duty, "gain": self.gain, "offset": self.offset,
                "tau_ms": round(self.tau_s * 1000.0, 2)}
