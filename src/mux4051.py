# mux4051.py
import time
import board
import digitalio
import analogio

def _gp(n):
    return getattr(board, "GP%d" % n)

class Mux4051:
    def __init__(self, common, a, b, c, inh=None, settle_s=0.001):
        self.adc = analogio.AnalogIn(_gp(common))
        self.a = digitalio.DigitalInOut(_gp(a)); self.a.switch_to_output()
        self.b = digitalio.DigitalInOut(_gp(b)); self.b.switch_to_output()
        self.c = digitalio.DigitalInOut(_gp(c)); self.c.switch_to_output()
        self.inh = None
        if inh is not None:
            self.inh = digitalio.DigitalInOut(_gp(inh))
            self.inh.switch_to_output(value=False)
        self.settle_s = settle_s

    def select(self, y):
        y = int(y) & 7
        self.a.value = bool(y & 1)
        self.b.value = bool(y & 2)
        self.c.value = bool(y & 4)

    def read(self, y):
        self.select(y)
        time.sleep(self.settle_s)
        _ = self.adc.value
        return self.adc.value