# tests for threshold_refresh.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import channel_gate
import rc_timing
import threshold_refresh
import tower_build


@case
def test_sample_hold_numbers():
    assert abs(threshold_refresh.SHARE_FRAC - 0.0909) < 1e-3
    assert abs(rc_timing.refresh_interval_s() - 0.020) < 1e-9       # 5 mV, 25 nA
    assert abs(rc_timing.refresh_interval_s(bias_a=250e-9) - 0.002) < 1e-9


@case
def test_refresher_round_robin_same_target():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    gate = channel_gate.ChannelGate(4)
    for i in (0, 1, 3):
        gate.set_kind(i, "head_new")
    gate.set_kind(2, "fault")
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, gate=gate,
                             clock=bank.clock)
    for b in bells:
        r.set_target(b, 20000)
    seen = [r.step() for _ in range(6)]
    assert seen == [0, 1, 3, 0, 1, 3], seen            # fault ch skipped
    assert abs(r.cycle_s() - 0.003) < 1e-9
    assert r.stale() == []                             # all refreshed < 20 ms
    bank.now += 0.05
    assert sorted(r.stale()) == [0, 1, 3]
    assert tower.holder is None


@case
def test_refresher_skips_busy_tower():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.set_target(bells[1], 1000)
    with tower.select(bells[0]):                       # e.g. a laser probe
        assert r.step() is None and r.skipped_busy == 1
        assert tower.current == 0
    assert r.step() == 1


@case
def test_refresher_retarget_settles_rc():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.set_target(bells[0], 40000)
    t0 = bank.now
    r.step()
    assert bank.now - t0 > 0.1                         # RC re-settle paid


@case
def test_droop_estimate_from_share_step():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    node = {"v": 30000.0}
    hold = {1: 30000.0 + 1100}                         # ch1 drooped up 1100

    class Node:
        @property
        def value(self):
            ch = tower.current
            if ch in hold and tower.holder == ch:
                return int(node["v"] + threshold_refresh.SHARE_FRAC * (hold[ch] - node["v"]))
            return int(node["v"])
    bells[0].drive.back = Node()
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    with tower.select(bells[0]):
        pass
    est = r.droop_estimate(bells[1])
    assert abs(est - 1100) < 15, est


@case
def test_refresh_rewrites_target_slow_update_changes_it():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.update_targets(30000)                          # slow: same for all
    assert r.target_updates == 4
    for _ in range(8):                               # fast: two cycles
        r.step()
    assert r.target == {0: 30000, 1: 30000, 2: 30000, 3: 30000}
    assert r.target_updates == 4                     # refresh never retargets
    r.update_targets(30000)                          # unchanged: no update
    assert r.target_updates == 4
    r.update_targets({2: 31000})                     # slow drift on one ch
    assert r.target_updates == 5 and r.target[2] == 31000
    seen = {}
    for _ in range(4):
        i = r.step()
        seen[i] = bells[0].drive.duty
    assert seen[2] == 31000 and seen[3] == 30000, seen


@case
def test_refresher_measured_interval_overrides_datasheet():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    assert r.interval_source == "datasheet" and abs(r.interval_s - 0.02) < 1e-9
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock,
                             interval_s=0.5)
    assert r.interval_source == "measured" and r.interval_s == 0.5
    assert abs(r.datasheet_interval_s - 0.02) < 1e-9
    prop = load_json("settings_proposed.json")["pins"]["threshold_mux"]
    r = threshold_refresh.refresher_from_cfg(tower, bells, prop, clock=bank.clock)
    assert r.dwell_s == 0.001
    exp = prop["refresh"].get("interval_s")
    assert r.interval_source == ("datasheet" if exp is None else "measured")


finish()
