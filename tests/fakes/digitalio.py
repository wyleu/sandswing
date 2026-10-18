import _fake_hw


class Direction:
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"


class Pull:
    UP = "UP"
    DOWN = "DOWN"


class DigitalInOut(_fake_hw.Claimed):
    def __init__(self, pin):
        _fake_hw.claim(pin, "DigitalInOut")
        self._pin = pin
        self._gp = pin.gp
        self.direction = Direction.INPUT
        self.pull = None
        self._v = False

    @property
    def value(self):
        if self.direction == Direction.INPUT:
            fn = _fake_hw.din_fn.get(self._gp)
            if fn is not None:
                return bool(fn())
            return self.pull == Pull.UP     # nothing connected: idle level
        return self._v

    @value.setter
    def value(self, v):
        self._v = bool(v)

    def switch_to_output(self, value=False, drive_mode=None):
        self.direction = Direction.OUTPUT
        self._v = bool(value)

    def switch_to_input(self, pull=None):
        self.direction = Direction.INPUT
        self.pull = pull
