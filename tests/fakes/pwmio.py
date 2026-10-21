import _fake_hw


class PWMOut(_fake_hw.Claimed):
    def __init__(self, pin, frequency=500, duty_cycle=0, variable_frequency=False):
        _fake_hw.claim(pin, "PWMOut@%d" % frequency)
        self._pin = pin
        self.frequency = frequency
        self.duty_cycle = duty_cycle
        _fake_hw.pwm[pin.gp] = self

    @property
    def duty_cycle(self):
        return self._duty

    @duty_cycle.setter
    def duty_cycle(self, d):
        self._duty = d
        _fake_hw.duty_log.append((self._pin.gp, d))

    def deinit(self):
        if self._pin is not None and _fake_hw.pwm.get(self._pin.gp) is self:
            del _fake_hw.pwm[self._pin.gp]
        _fake_hw.Claimed.deinit(self)
