# tests for loopback_test.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import loopback_test


@case
def test_loopback_threshold_path_classifies():
    random.seed(1)
    tower, bells, _ = rig(plug_a={0, 1}, plug_b={0, 1})
    st = loopback_test.LoopbackSelfTest(tower)
    r0 = st.threshold_loopback(bells[0])
    assert r0["ok"] and r0["plug"] is True, r0
    r2 = st.threshold_loopback(bells[2])
    assert r2["ok"] and r2["plug"] is False, r2
    assert tower.holder is None and bells[0].drive.duty == 0


@case
def test_loopback_broken_switch_is_fault():
    tower, bells, plant = rig(plug_a={1}, plug_b={1}, ron_ratio=0.3)   # below bench-tuned LB_RATIO_MIN 0.5
    r = loopback_test.LoopbackSelfTest(tower).threshold_loopback(bells[1])
    assert not r["ok"] and r["plug"] is None, r


@case
def test_loopback_digital_and_sweep():
    random.seed(2)
    tower, bells, _ = rig(plug_a={0}, plug_b={0})
    st = loopback_test.LoopbackSelfTest(tower)
    d = st.digital_loopback(bells[0])
    assert d["ok"] and d["seen"] == [False, True, False], d
    s = st.duty_sweep(bells[0])
    assert s["ok"] and s["monotonic"], s
    n = st.digital_loopback(bells[3])           # no plug A on ch3
    assert n["plug"] is False and not n["ok"]


@case
def test_dac_linearity_on_unloaded_channel():
    tower, bells, _ = rig(plug_a=set(), plug_b={0})
    r = loopback_test.LoopbackSelfTest(tower).dac_linearity(bells[2])
    assert r["ok"] and abs(r["gain"] - 0.98) < 0.01, r


@case
def test_full_run_report():
    random.seed(3)
    tower, bells, _ = rig(plug_a={0, 1}, plug_b={0, 1})
    rep = loopback_test.LoopbackSelfTest(tower).run(bells)
    assert rep["ok"], rep
    assert rep["channels"][0]["threshold"]["plug"] is True
    assert "digital" not in rep["channels"][3]
    assert rep["dac"]["ch"] == 2


class IdleHighPin(object):
    """Blue/White with nothing driving it but the pull-up (trigger plug,
    switch open)."""
    value = True


@case
def test_trigger_plug_in_selftest_is_not_a_failure():
    random.seed(4)
    tower, bells, _ = rig(plug_a={0}, plug_b={0, 1})
    bells[1].sense_pin = IdleHighPin()          # ch2: trigger plug, switch open
    rep = loopback_test.LoopbackSelfTest(tower).run(bells)
    d = rep["channels"][1]["digital"]
    assert d.get("trigger") and d["ok"] is None, d
    assert "sweep" not in rep["channels"][1]
    assert rep["ok"], rep


class LowPin(object):
    value = False                               # switch to 0 V held


@case
def test_trigger_plug_held_to_ground_in_selftest_is_not_a_failure():
    random.seed(5)
    tower, bells, _ = rig(plug_a={0}, plug_b={0, 1})
    bells[1].sense_pin = LowPin()
    rep = loopback_test.LoopbackSelfTest(tower).run(bells)
    assert rep["channels"][1]["digital"].get("trigger"), rep["channels"][1]
    assert rep["ok"], rep


finish()
