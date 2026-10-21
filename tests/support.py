# support.py -- shared fakes and rigs for the sandswing_next unit tests.
# Plain CPython; no board modules needed (pins come from FakePinBank).
import asyncio
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
import mux_select
import tower_build

class FakePinBank:
    """Emulates CircuitPython: claiming a claimed GP raises ValueError."""
    def __init__(self):
        self.claimed = {}
        self.log = []
        self.slept = []

    def _claim(self, gp, obj):
        gp = int(gp)
        if gp in self.claimed:
            raise ValueError("GP%d in use" % gp)
        self.claimed[gp] = obj
        obj.gp = gp
        obj.bank = self
        return obj

    def output(self, gp, value=False):
        return self._claim(gp, FakeOut(value))

    def analog_in(self, gp):
        adc = FakeADC()
        if int(gp) == 28:                      # RC node on output-4051 COMMON
            adc = FakeRCNode(self)
        return self._claim(gp, adc)

    def pwm_out(self, gp, frequency=1000, duty_cycle=0):
        pwm = FakePWM(duty_cycle)
        pwm.frequency = frequency
        if int(gp) == 21:
            self.thr_pwm = pwm
        return self._claim(gp, pwm)

    now = 0.0

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def clock(self):
        return self.now


class _Fake:
    def deinit(self):
        self.bank.claimed.pop(self.gp, None)


class FakeOut(_Fake):
    def __init__(self, v):
        self._v = v

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        self._v = bool(v)
        self.bank.log.append((self.gp, self._v))


class FakeADC(_Fake):
    value = 1234


class FakeRCNode(_Fake):
    """GP28 readback: follows the GP21 duty with optional gain/offset/fault."""
    def __init__(self, bank):
        self._bank = bank
        self.gain, self.offset, self.stuck = 0.98, 300, None

    @property
    def value(self):
        if self.stuck is not None:
            return self.stuck
        return int(self.offset + self.gain * self._bank.thr_pwm.duty_cycle)


class FakePWM(_Fake):
    def __init__(self, d):
        self.duty_cycle = d


PINS = {   # same shape as mux_blend settings_example.json
    "sense": [0, 1, 2, 3],
    "adc_mux": {"common": 26, "a": 18, "b": 19, "c": 20, "inh": None,
                "laser_y": [0, 1, 2, 3, 4, 5, 6, 7]},
    "threshold_mux": {"a": 18, "b": 19, "c": 20, "inh": None,
                      "y": [0, 1, 2, 3, 4, 5, 6, 7], "pwm_gp": 21,
                      "sense_gp": 28},
}

NEW_STYLE = {
    "sense": [0, 1, 2, 3],
    "select_groups": {"tower": {"a": 18, "b": 19, "c": 20, "inh": 22,
                                "settle_us": 500}},
    "adc_mux": {"select": "tower", "common": 26, "y": [0, 1, 2, 3]},
    "threshold_mux": {"select": "tower", "pwm_gp": 21, "sense_gp": 28},
}

results = []


def case(fn):
    mux_select.reset_registry()
    try:
        fn()
        results.append((fn.__name__, True, ""))
        print("  ok ", fn.__name__)
    except Exception as e:
        results.append((fn.__name__, False, repr(e)))
        print("  FAIL", fn.__name__, repr(e))


def skip(name, why):
    """Record a check that cannot run (e.g. a known firmware bug)."""
    results.append((name, None, why))
    print("  skip ", name, " -- skipped:", why)


def run(coro):
    """Run one coroutine (async survey/classify) to completion."""
    return asyncio.run(coro)


def expect(exc, f, *a):
    try:
        f(*a)
    except exc:
        return
    raise AssertionError("expected %s" % exc.__name__)


def load_json(name):
    with open(os.path.join(HERE, "data", name)) as f:
        return json.load(f)


def finish():
    failed = [r for r in results if r[1] is False]
    skipped = [r for r in results if r[1] is None]
    print("\n%d passed, %d failed, %d skipped"
          % (len(results) - len(failed) - len(skipped), len(failed), len(skipped)))
    sys.exit(1 if failed else 0)




class Plant:
    """Per-channel plugs. A: laser GPn -> sense. B: Orange -> Green/White."""
    def __init__(self, bank, tower, plug_a, plug_b, ron_ratio=0.99,
                 broken_b=(), dac_gain=0.98, dac_off=200):
        self.bank, self.tower = bank, tower
        self.plug_a, self.plug_b, self.broken_b = plug_a, plug_b, set(broken_b)
        self.ron_ratio, self.g, self.o = ron_ratio, dac_gain, dac_off
        self.lasers = {}

    def duty(self):
        return self.bank.thr_pwm.duty_cycle

    def looped(self):
        ch = self.tower.current
        return ch in self.plug_b and ch not in self.broken_b

    def gp28(self):
        load = 0.835 if self.looped() else 1.0
        return int(self.o + self.g * self.duty() * load)

    def gp26(self):
        if self.looped():
            return int(self.gp28() * self.ron_ratio)
        return 700


class PlantADC:
    def __init__(self, fn):
        self.fn = fn

    @property
    def value(self):
        return self.fn()

    def deinit(self):
        pass


class SensePin:
    def __init__(self, plant, ch):
        self.plant, self.ch = plant, ch

    @property
    def value(self):
        if self.ch not in self.plant.plug_a:
            return False                       # head at rest / pulled low
        d = self.plant.lasers[self.ch].duty_cycle
        if d in (0, 65535):
            return d == 65535
        return random.random() < d / 65535.0


def rig(plug_a, plug_b, **kw):
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    plant = Plant(bank, tower, plug_a, plug_b, **kw)
    bells[0].drive.back = PlantADC(plant.gp28)
    bells[0].sense.adc = PlantADC(plant.gp26)
    for b in bells:
        b.laser = bank.pwm_out(8 + b.index)
        plant.lasers[b.index] = b.laser
        b.sense_pin = SensePin(plant, b.index)
    return tower, bells, plant


BASE = dict(l0=0.0, l50=1.0, r50=0.02, dark=700, di_ma=0.0, x=0.0,
            follow=False, pull_follow=True, thr_toggle=False)


def sig(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


class PullSensePin(SensePin):
    pull = "down"

    @property
    def value(self):
        if self.ch not in self.plant.plug_a:
            return self.pull == "up"
        return SensePin.value.fget(self)


