# test_pt_level.py -- channel 1's Orange/White PT level on GP27 (no mux)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import *  # noqa: E402
import pt_level
from pt_level import PtLevel, open_pt_level, pt_settings, ADC_FULL, PT_N
from signal_bands import VREF


class Cfg(dict):
    pass


class SeqADC:
    def __init__(self, values):
        self.values = list(values)
        self.i = 0
        self.closed = False

    @property
    def value(self):
        v = self.values[self.i % len(self.values)]
        self.i += 1
        return v

    def deinit(self):
        self.closed = True


@case
def test_defaults_are_gp27_channel_1_when_settings_say_nothing():
    assert pt_settings(Cfg(pins={"sense": [0]}), 8) == ((27, 0), None)
    assert pt_settings(Cfg(), 8) == ((27, 0), None)


@case
def test_settings_can_move_it_or_switch_it_off_and_bad_values_claim_nothing():
    assert pt_settings(Cfg(pins={"pt_level": {"gp": 29, "ch": 3}}), 8) == ((29, 2), None)
    for off in (False, None, {}):
        got, why = pt_settings(Cfg(pins={"pt_level": off}), 8)
        assert got is None and "off" in why, (off, why)
    for bad in ({"gp": 21}, {"gp": "27"}, {"gp": True}, {"ch": 0}, {"ch": 9}, [27]):
        got, why = pt_settings(Cfg(pins={"pt_level": bad}), 8)
        assert got is None and why, bad


@case
def test_read_gives_mean_volts_and_spread_over_the_burst():
    p = PtLevel(SeqADC([10000, 30000]), 0, 27)
    v, span = p.read()
    assert abs(v - round(20000 * VREF / ADC_FULL, 3)) < 1e-9, v
    assert abs(span - round(20000 * VREF / ADC_FULL, 3)) < 1e-9, span
    adc = SeqADC([5000])
    p = PtLevel(adc, 0, 27)
    assert p.read()[1] == 0.0 and adc.i == PT_N


@case
def test_only_the_wired_channel_has_a_level():
    p = PtLevel(SeqADC([40000]), 0, 27)
    assert p.for_ch(1) is None and p.for_ch(0) is not None
    assert p.info()["ch"] == 1 and p.info()["gp"] == 27 and "Orange/White" in p.info()["valid"]


@case
def test_a_failing_adc_reads_none_and_counts():
    class Bad:
        @property
        def value(self):
            raise OSError("adc")
    p = PtLevel(Bad(), 0, 27)
    assert p.read() is None and p.errors == 1


@case
def test_open_claims_gp27_once_and_a_claimed_pin_is_logged_not_raised():
    bank = FakePinBank()
    lines = []
    p = open_pt_level(Cfg(), bank, 8, log=lines.append)
    assert p is not None and 27 in bank.claimed and p.ch == 0
    assert open_pt_level(Cfg(), bank, 8, log=lines.append) is None    # GP27 in use
    assert any("not claimed" in l for l in lines), lines
    p.close()
    assert 27 not in bank.claimed
    assert open_pt_level(Cfg(pins={"pt_level": False}), FakePinBank(), 8,
                         log=lines.append) is None


finish()
