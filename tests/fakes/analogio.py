import _fake_hw


class AnalogIn(_fake_hw.Claimed):
    """GP28 follows the GP21 threshold PWM duty (unloaded RC node);
    every other pin reads the empty-port floor (700) unless a test sets
    _fake_hw.adc_fn[gp]."""
    def __init__(self, pin):
        _fake_hw.claim(pin, "AnalogIn")
        self._pin = pin
        self._gp = pin.gp
        self.reference_voltage = 3.3

    @property
    def value(self):
        fn = _fake_hw.adc_fn.get(self._gp)
        if fn is not None:
            return int(fn())
        if self._gp == 28 and 21 in _fake_hw.pwm:
            return int(_fake_hw.pwm[21].duty_cycle)
        return 700
