# tests for tower_build.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import connector_boards
import mux_errors
import mux_select
import tower_build


@case
def test_legacy_settings_share_one_tower():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    assert len(bells) == 4
    assert all(b.tower is tower for b in bells)
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28], sorted(bank.claimed)


@case
def test_new_style_with_inh():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(NEW_STYLE, bank)
    assert 22 in bank.claimed and tower.settle_s == 0.0005
    with tower.select(bells[3]):
        assert tower.current == 3
    # INH went high before A/B/C changed and low after (break-before-make)
    seq = [g for g, _ in bank.log]
    assert seq[0] == 22 and seq[-1] == 22, bank.log


@case
def test_mismatched_y_rejected():
    bank = FakePinBank()
    bad = dict(PINS)
    bad["threshold_mux"] = dict(PINS["threshold_mux"], y=[1, 0, 2, 3])
    expect(mux_errors.MuxConfigError, tower_build.build_from_pins, bad, bank)


@case
def test_different_abc_rejected():
    bank = FakePinBank()
    bad = dict(PINS)
    bad["threshold_mux"] = dict(PINS["threshold_mux"], a=17)
    expect(mux_errors.MuxConfigError, tower_build.build_from_pins, bad, bank)


@case
def test_gp27_never_claimed_and_warned():
    bank = FakePinBank()
    legacy = dict(PINS)
    legacy["threshold_mux"] = dict(PINS["threshold_mux"], common=27)
    tower, _ = tower_build.build_from_pins(legacy, bank)
    assert 27 not in bank.claimed and tower.warnings, tower.warnings


@case
def test_default_config_is_8_channels_1_to_4_populated():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(None, bank)
    assert len(bells) == 8 and [b.y for b in bells] == list(range(8))
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28], sorted(bank.claimed)
    assert connector_boards.POPULATED == [0, 1, 2, 3]
    assert bells[0].drive.pwm.frequency == 20000
    out = connector_boards.apply_boards({c: "empty" for c in range(8)},
                         tower_build.DEFAULT_PINS["connector_boards"])
    assert [out[c] for c in range(8)] == ["empty"] * 4 + ["no_board"] * 4


@case
def test_current_board_settings_legacy():
    cur = load_json("settings_current.json")
    # what mux_blend does with it: Mux4051 x2 on the same a/b/c
    bank = FakePinBank()
    for gp in (26, 18, 19, 20):                       # claim_muxes: adc_mux
        bank.output(gp) if gp != 26 else bank.analog_in(gp)
    bank.analog_in(27)                                # thr Mux4051 common
    expect(ValueError, bank.output, 18)               # -> "GP18 in use"
    # the draft with the same file
    mux_select.reset_registry()
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(cur, bank)
    assert len(bells) == 8 and [b.y for b in bells] == list(range(8))
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28], sorted(bank.claimed)
    assert 27 not in bank.claimed and any("27" in w for w in tower.warnings)


@case
def test_proposed_settings_build():
    prop = load_json("settings_proposed.json")
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(prop, bank)
    assert len(bells) == 8 and not tower.warnings
    assert "fitted" not in prop["pins"]
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28]
    assert bells[0].drive.pwm.frequency == 20000
    assert abs(bells[0].drive.tau_s - 0.0205) < 0.0005
    out = connector_boards.apply_boards({c: "empty" for c in range(8)},
                         prop["pins"]["connector_boards"])
    assert [out[c] for c in range(4, 8)] == ["no_board"] * 4


@case
def test_proposed_settings_single_threshold_pwm_hz():
    prop = load_json("settings_proposed.json")
    thr = prop["pins"]["threshold_mux"]
    assert thr["pwm_hz"] == 20000 and thr["pwm_gp"] == 21
    flat = json.dumps(prop)
    assert flat.count('"pwm_hz"') == 1, "one threshold PWM frequency key"


finish()
