import _fake_hw

GRB = "GRB"
RGB = "RGB"


class NeoPixel(_fake_hw.Claimed):
    def __init__(self, pin, n, brightness=1.0, auto_write=True, pixel_order=None):
        _fake_hw.claim(pin, "NeoPixel")
        self._pin = pin
        self.n = n
        self.brightness = brightness
        self._px = [(0, 0, 0)] * n

    def __len__(self):
        return self.n

    def __setitem__(self, i, v):
        self._px[i] = v

    def __getitem__(self, i):
        return self._px[i]

    def fill(self, v):
        self._px = [v] * self.n

    def show(self):
        pass
