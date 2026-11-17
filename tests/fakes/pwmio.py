import _fake_hw

# CircuitPython 10.0.3 rp2 (ports/raspberrypi/common-hal/pwmio/PWMOut.c):
# a slice (gp >> 1 & 7) has ONE frequency.  A second channel on a slice
# must ask for the same frequency, variable_frequency is refused on a slice
# already in use (and blocks the other channel), and .frequency can only be
# written with variable_frequency.  The slice is free when both are deinit.
slice_hz = {}        # slice -> frequency
slice_var = set()    # slices with a variable_frequency user
slice_users = {}     # slice -> number of live PWMOuts


def _slice(gp):
    return (gp >> 1) & 7


class PWMOut(_fake_hw.Claimed):
    def __init__(self, pin, frequency=500, duty_cycle=0, variable_frequency=False):
        sl = _slice(pin.gp)
        if sl in slice_hz:
            if variable_frequency:
                raise ValueError("Invalid variable_frequency")
            if sl in slice_var:
                raise RuntimeError("Internal resource(s) in use")
            if slice_hz[sl] != frequency:
                raise ValueError("Invalid frequency")
        _fake_hw.claim(pin, "PWMOut@%d" % frequency)
        self._pin = pin
        self._slice = sl
        self._var = variable_frequency
        self._frequency = frequency
        slice_hz[sl] = frequency
        slice_users[sl] = slice_users.get(sl, 0) + 1
        if variable_frequency:
            slice_var.add(sl)
        self.duty_cycle = duty_cycle
        _fake_hw.pwm[pin.gp] = self

    @property
    def frequency(self):
        return self._frequency

    @frequency.setter
    def frequency(self, f):
        if not self._var:
            raise AttributeError("PWM frequency not writable when variable_frequency is False")
        self._frequency = f
        slice_hz[self._slice] = f

    @property
    def duty_cycle(self):
        return self._duty

    @duty_cycle.setter
    def duty_cycle(self, d):
        self._duty = d
        _fake_hw.duty_log.append((self._pin.gp, d))

    def deinit(self):
        if self._pin is not None:
            if _fake_hw.pwm.get(self._pin.gp) is self:
                del _fake_hw.pwm[self._pin.gp]
            sl = self._slice
            slice_users[sl] -= 1
            if not slice_users[sl]:
                del slice_users[sl]
                slice_hz.pop(sl, None)
                slice_var.discard(sl)
        _fake_hw.Claimed.deinit(self)
