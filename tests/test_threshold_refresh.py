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


@case
def test_threshold_settings_from_cfg_and_defaults():
    s = threshold_refresh.threshold_settings(None)
    assert s == {"default_duty": threshold_refresh.DEFAULT_DUTY,
                 "min_duty": threshold_refresh.MIN_DUTY,
                 "max_duty": threshold_refresh.MAX_DUTY}
    s = threshold_refresh.threshold_settings(
        {"threshold": {"default_duty": 40000, "min_duty": 1000, "max_duty": 50000}})
    assert s == {"default_duty": 40000, "min_duty": 1000, "max_duty": 50000}
    s = threshold_refresh.threshold_settings(
        {"threshold": {"default_duty": 10, "min_duty": 100, "max_duty": 200}})
    assert s["default_duty"] == 100                     # clamped to min
    s = threshold_refresh.threshold_settings(
        {"threshold": {"default_duty": "x", "min_duty": 200, "max_duty": 100}})
    assert s["min_duty"] == 100 and s["max_duty"] == 200  # swapped
    assert s["default_duty"] == threshold_refresh.clamp_duty(
        threshold_refresh.DEFAULT_DUTY, 100, 200)
    import json, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = json.load(open(os.path.join(root, "settings_example.json")))["threshold"]
    import config_loader
    assert ex == config_loader.DEFAULTS["threshold"]
    assert ex == {"default_duty": 32768, "min_duty": 0, "max_duty": 65535}


@case
def test_set_target_clamps_to_settings_limits():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(
        tower, bells[0].drive, bells, clock=bank.clock, min_duty=1000, max_duty=40000)
    assert r.set_target(bells[0], 0) == 1000
    assert r.set_target(bells[0], 65535) == 40000
    assert r.target[0] == 40000


@case
def test_seed_kinds_only_new_heads_and_keeps_an_existing_target():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    gate = channel_gate.ChannelGate(4)
    gate.set_kind(0, "head_new")
    gate.set_kind(1, "head_old")
    gate.set_kind(2, "empty")
    gate.set_kind(3, "head_new")
    r = threshold_refresh.ThresholdRefresher(
        tower, bells[0].drive, bells, gate=gate, clock=bank.clock, default_duty=22222)
    r.set_target(bells[3], 40000)                      # already tuned
    r.seed_kinds({0: "head_new", 1: "head_old", 2: "empty", 3: "head_new"})
    assert r.target == {0: 22222, 3: 40000}, r.target  # 3 kept, 1 and 2 not seeded
    r.seed_kinds({0: "empty", 1: "head_old", 2: "empty", 3: "head_new"})
    assert r.target == {3: 40000}                      # 0 dropped when no longer new
    assert 0 not in r.readback


@case
def test_apply_once_writes_each_target_then_stops():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    gate = channel_gate.ChannelGate(4)
    gate.set_kind(0, "head_new")
    gate.set_kind(1, "head_new")
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, gate=gate,
                                             clock=bank.clock)
    r.seed_kinds({0: "head_new", 1: "head_new", 2: "empty", 3: "empty"})
    done = r.apply_once()
    assert sorted(done) == [0, 1], done
    assert sorted(r.readback) == [0, 1]
    done2 = r.apply_once()
    assert sorted(done2) == [0, 1]


@case
def test_step_records_gp28_readback():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.set_target(bells[0], 20000)
    assert r.step() == 0
    got = r.readback[0]
    # FakeRCNode: offset + gain * duty
    assert abs(got - int(300 + 0.98 * 20000)) < 2, got
    st = r.serial_thr(4)
    assert st["target"] == [20000, None, None, None]
    assert st["readback"][0] == got and st["readback"][1] is None


@case
def test_run_survives_a_failed_verify_and_does_not_block_again():
    # A GP28 mismatch raised out of run() would end main() -> Pico reset.
    import asyncio
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    bells[0].drive.back.stuck = 5                      # readback far from the duty
    r.set_target(bells[0], 40000)

    async def go():
        t = asyncio.ensure_future(r.run())
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return t
    asyncio.run(go())
    assert r.errors == 1 and "ThresholdMismatch" in r.last_error, (r.errors, r.last_error)
    slept = len(bank.slept)
    r.step()
    assert len(bank.slept) - slept == 1                # duty already set: only the dwell
    assert tower.holder is None
    assert r.status()["errors"] == 1
    assert 0 in r.readback                             # recorded even on the failed verify


finish()
