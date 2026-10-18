# Shared state for the fake CircuitPython modules.
claimed = {}      # gp -> owner description
history = []      # (gp, kind) for every successful claim
errors = []       # every 'GPxx in use' raised (even if the caller swallowed it)
pwm = {}          # gp -> live PWMOut (for the GP28 RC-node fake)
wdt_log = []      # (what, value, claims so far) for the fake watchdog
din_fn = {}       # gp -> callable, test override of an input level
adc_fn = {}       # gp -> callable, test override of an AnalogIn value


def claim(pin, kind):
    gp = pin.gp
    if gp in claimed:
        msg = "GP%d in use" % gp
        errors.append((gp, kind, claimed[gp]))
        raise ValueError(msg)
    claimed[gp] = kind
    history.append((gp, kind))


def release(pin):
    claimed.pop(pin.gp, None)


class Claimed:
    _pin = None

    def deinit(self):
        if self._pin is not None:
            release(self._pin)
            self._pin = None
