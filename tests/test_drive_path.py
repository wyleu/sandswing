# tests for drive_path.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import mux_errors
import tower_build


@case
def test_drive_waits_rc_and_verifies_readback():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    d = bells[0].drive
    assert bank.thr_pwm.frequency == 20000
    with tower.select(bells[0]):
        got = bells[0].set_drive(32768)
        assert abs(got - 32768) < 1024, got
        half = d.last_settle_s
        assert 0.11 < half < 0.16, half         # 50 % step -> ~157 ms
        bells[0].set_drive(32768)               # no change -> no wait
        assert d.last_settle_s == 0.0
    assert any(abs(x - half) < 1e-9 for x in bank.slept)


@case
def test_readback_fault_raises():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    bank.claimed[28].stuck = 0                  # open RC / dead PWM
    with tower.select(bells[1]):
        expect(mux_errors.ThresholdMismatch, bells[1].set_drive, 40000)


@case
def test_calibrate_absorbs_gain_offset():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    node = bank.claimed[28]
    node.gain, node.offset = 0.9, 1500          # outside default tol at top
    d = bells[2].drive
    with tower.select(bells[2]):
        expect(mux_errors.ThresholdMismatch, bells[2].set_drive, 60000)
        g, o = d.calibrate(bells[2].y)
        assert abs(g - 0.9) < 0.01 and abs(o - 1500) < 50, (g, o)
        bells[2].set_drive(60000)


@case
def test_drive_needs_selection():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    expect(mux_errors.NotSelected, bells[0].set_drive, 100)


finish()
