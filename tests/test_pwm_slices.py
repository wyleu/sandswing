# tests for pwm_slices.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import mux_errors
import mux_select
import pwm_slices
import tower_build


@case
def test_pwm_slice_formula():
    # lasers GP8..15 -> 4A 4B 5A 5B 6A 6B 7A 7B; threshold GP21 -> 2B
    got = [pwm_slices.pwm_slice(g) for g in range(8, 16)]
    assert got == [(4, "A"), (4, "B"), (5, "A"), (5, "B"),
                   (6, "A"), (6, "B"), (7, "A"), (7, "B")], got
    assert pwm_slices.pwm_slice(21) == (2, "B")
    assert pwm_slices.pwm_slice(28) == (6, "A") == pwm_slices.pwm_slice(12)
    assert pwm_slices.pwm_slice(0) == (0, "A") and pwm_slices.pwm_slice(16) == (0, "A")
    assert pwm_slices.pwm_slice(29) == (6, "B")
    expect(mux_errors.PwmSliceError, pwm_slices.pwm_slice, 30)       # not on RP2350A


@case
def test_pwm_gp12_gp28_clash_fails():
    try:
        pwm_slices.check_pwm_slices([(12, 1000, "laser ch5"), (28, 20000, "threshold")])
    except mux_errors.PwmSliceError as e:
        assert "slice 6 channel A" in str(e), str(e)
    else:
        raise AssertionError("GP12 + GP28 must clash")
    # and through build_from_pins, before any pin is claimed
    bad = json.loads(json.dumps(load_json("settings_proposed.json")))
    bad["pins"]["threshold_mux"]["pwm_gp"] = 28
    bank = FakePinBank()
    expect(mux_errors.PwmSliceError, tower_build.build_from_pins, bad, bank)
    assert bank.claimed == {}, bank.claimed


@case
def test_pwm_gp21_with_lasers_passes():
    pins = [(g, 1000, "laser") for g in range(8, 16)] + [(21, 20000, "thr")]
    table, warn = pwm_slices.check_pwm_slices(pins)
    assert warn == [] and table[21][:2] == (2, "B")
    assert sorted(table) == list(range(8, 16)) + [21]
    bank = FakePinBank()
    tower, _ = tower_build.build_from_pins(load_json("settings_proposed.json"), bank)
    assert tower.pwm_slices[21][:3] == (2, "B", 20000)
    assert tower.pwm_slices[12][:2] == (6, "A")


@case
def test_pwm_same_slice_other_channel_ok_same_freq():
    table, warn = pwm_slices.check_pwm_slices({8: 1000, 9: 1000})     # 4A + 4B
    assert warn == []
    expect(mux_errors.PwmSliceError, pwm_slices.check_pwm_slices, [8, 8])      # listed twice


@case
def test_pwm_shared_slice_freq_mismatch():
    # GP20 is 2A, GP21 is 2B: different channels, but one slice -> one freq
    expect(mux_errors.PwmSliceError, pwm_slices.check_pwm_slices, {20: 1000, 21: 20000})
    _, warn = pwm_slices.check_pwm_slices({20: 1000, 21: 20000}, freq_mismatch="warn")
    assert len(warn) == 1 and "slice 2" in warn[0], warn
    # through build_from_pins: a laser moved onto GP20 (2A, 1 kHz) would
    # share slice 2 with the 20 kHz threshold PWM on GP21 (2B)
    cfg = json.loads(json.dumps(load_json("settings_proposed.json")))
    cfg["pins"]["laser_pwm"] = [8, 9, 10, 11, 12, 13, 14, 20]   # 20 = 2A
    bank = FakePinBank()
    expect(mux_errors.PwmSliceError, tower_build.build_from_pins, cfg, bank)
    mux_select.reset_registry()
    bank = FakePinBank()
    tower, _ = tower_build.build_from_pins(cfg, bank, freq_mismatch="warn")
    assert any("frequency" in w for w in tower.warnings), tower.warnings


finish()
