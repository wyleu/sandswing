# sense_path.py -- inward (current-sense) 4051: COMMON -> GP26 AnalogIn.
# Borrows the tower selection; owns nothing (adc injected).
from mux_errors import NotSelected


class SensePath:
    def __init__(self, tower, adc):
        self.tower = tower
        self.adc = adc

    def read(self, y, discard=1):
        if not self.tower.is_held_by(y):
            raise NotSelected("sense read ch %d without tower.select" % int(y))
        for _ in range(discard):
            _ = self.adc.value          # first sample after switch is stale
        return self.adc.value
