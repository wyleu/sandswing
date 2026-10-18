# tests for laser_measure.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import laser_measure
import signal_bands
import tower_build


@case
def test_laser_presence_bands():
    k = signal_bands.COUNTS_PER_MA
    assert abs(k - 297.9) < 0.5, k
    # mentor: ~5 mA module -> ~75 mV -> ~1490 counts (16-bit scale)
    assert abs(5 * k - 1490) < 5
    assert laser_measure.classify_laser(700, 700 + 1490)[0] == "ok"
    assert laser_measure.classify_laser(700, 700 + 400)[0] == "open"        # <2 mA
    assert laser_measure.classify_laser(700, 700 + int(70 * k))[0] == "over"
    assert laser_measure.classify_laser(2500, 4000)[0] == "stuck_on"
    st, i_on, _ = laser_measure.classify_laser(700, 9100)                    # README bench
    assert st == "ok" and 27 < i_on < 29
    assert laser_measure.classify_laser(700, 700 + int(15 * k), ref_ma=28.0)[0] == "drift"


@case
def test_measure_laser_uses_selection_and_leaves_off():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    laser = bank.pwm_out(8)
    class CurrentADC:
        @property
        def value(self):
            assert tower.is_held_by(bells[0].y)
            return 700 + (1490 if laser.duty_cycle == 65535 else 0)
    dark, on = laser_measure.measure_laser(tower, bells[0], laser, CurrentADC())
    assert (dark, on) == (700, 2190) and laser.duty_cycle == 0
    assert tower.holder is None


finish()
