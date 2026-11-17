# test_stuck.py -- a head held / flickering all the time is not a ringing bell:
# thr / drive commands go through again (stuck.py, tune.Tuner._refuse,
# align_scope FLAG_STUCK), and normal strokes still keep the gate shut.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import *  # noqa: E402
import channel_gate
import threshold_refresh
import tower_build
import tune
import heads_cc
import stuck
import align_scope as A
from ring import MIN_STROKE_GAP_MS
from tower_watch import RINGDOWN_S
from pt_level import PtLevel

AFTER_MS = stuck.STUCK_AFTER_S * stuck.MS_PER_S
CLEAR_MS = stuck.STUCK_CLEAR_MS
RING_DOWN_MS = RINGDOWN_S * stuck.MS_PER_S
CHOP_ON_MS, CHOP_OFF_MS = 3, 2      # a head flickering on its threshold / the laser chop
STROKE_HELD_MS = 60                 # a real bell: the strip passes in tens of ms ...
STROKE_EVERY_MS = 2000              # ... about every 2 s


class Clock:
    def __init__(self, t=1000):
        self.t = t

    def __call__(self):
        return self.t


def flicker(w, i, clock, ms):
    """Held CHOP_ON_MS, clear CHOP_OFF_MS, for ms."""
    end = clock.t + ms
    while clock.t < end:
        w.edge(i, True, clock.t)
        clock.t += CHOP_ON_MS
        w.edge(i, False, clock.t)
        clock.t += CHOP_OFF_MS


def ring(w, i, clock, ms):
    end = clock.t + ms
    while clock.t < end:
        w.edge(i, True, clock.t)
        clock.t += STROKE_HELD_MS
        w.edge(i, False, clock.t)
        clock.t += STROKE_EVERY_MS - STROKE_HELD_MS


@case
def test_settings_defaults_overrides_and_bad_values():
    assert stuck.stuck_settings(None) == (AFTER_MS, CLEAR_MS)
    assert CLEAR_MS == MIN_STROKE_GAP_MS and stuck.STUCK_AFTER_S == RINGDOWN_S
    assert stuck.stuck_settings({"tune": {"stuck_after_s": 4, "stuck_clear_ms": 100}}) == (4000, 100)
    for bad in (0, -1, "x", True, None, float("nan")):
        assert stuck.stuck_settings({"tune": {"stuck_after_s": bad, "stuck_clear_ms": bad}}) == (
            AFTER_MS, CLEAR_MS), bad


@case
def test_flicker_is_stuck_only_after_the_setting():
    c = Clock()
    logs = []
    w = stuck.StuckWatch(4, born_ms=c.t, log=logs.append)
    flicker(w, 0, c, AFTER_MS - 100)
    assert not w.stuck(0, c.t) and w.stuck_list(c.t) == []
    flicker(w, 0, c, 200)
    assert w.stuck(0, c.t) and w.stuck_list(c.t) == [1]
    assert len(logs) == 1 and logs[0].startswith("ch 1 stuck"), logs
    flicker(w, 0, c, 1000)
    assert len(logs) == 1                       # logged once per spell


@case
def test_a_ringing_bell_is_never_stuck():
    c = Clock()
    w = stuck.StuckWatch(4, born_ms=c.t, log=lambda s: None)
    ring(w, 0, c, 10 * AFTER_MS)
    assert not w.stuck(0, c.t)
    assert not w.allows(c.t)                    # and keeps the gate shut


@case
def test_held_with_no_edges_is_not_stuck_held_after_an_edge_is():
    c = Clock()
    w = stuck.StuckWatch(4, born_ms=c.t, log=lambda s: None)
    c.t += 5 * AFTER_MS
    assert w.stuck_list(c.t) == []              # no edge: not keeping the tower awake
    w.edge(1, True, c.t)
    c.t += AFTER_MS
    assert w.stuck_list(c.t) == [2]


@case
def test_a_first_stroke_long_after_boot_is_not_stuck():
    c = Clock()
    w = stuck.StuckWatch(4, born_ms=c.t, log=lambda s: None)
    c.t += 5 * AFTER_MS
    w.edge(2, True, c.t)
    assert not w.stuck(2, c.t)
    c.t += STROKE_HELD_MS
    w.edge(2, False, c.t)
    assert w.stuck_list(c.t) == []


@case
def test_a_long_clear_gap_ends_the_spell():
    c = Clock()
    w = stuck.StuckWatch(4, born_ms=c.t, log=lambda s: None)
    flicker(w, 0, c, AFTER_MS + 100)
    assert w.stuck(0, c.t)
    w.edge(0, False, c.t)
    c.t += CLEAR_MS - 1
    assert w.stuck(0, c.t)                      # a short gap is still a flicker
    c.t += 1
    assert not w.stuck(0, c.t)                  # clear for clear_ms: not stuck
    w.edge(0, True, c.t)
    assert not w.stuck(0, c.t)
    assert not w.flagged[0]


@case
def test_allows_only_when_every_awake_channel_is_stuck():
    c = Clock()
    w = stuck.StuckWatch(4, born_ms=c.t, log=lambda s: None)
    assert not w.allows(c.t)                    # nothing stuck: the old gate
    flicker(w, 0, c, AFTER_MS + 100)
    assert w.allows(c.t)
    w.edge(1, True, c.t)                        # a real stroke on ch 2
    w.edge(1, False, c.t + STROKE_HELD_MS)
    flicker(w, 0, c, RING_DOWN_MS - 100)
    assert not w.allows(c.t)                    # ch 2 rang inside the ring-down
    flicker(w, 0, c, 200)
    assert w.allows(c.t)


def host(clock):
    mux_select.reset_registry()
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    for i, b in enumerate(bells):
        b.laser = bank.pwm_out(8 + i, frequency=1000, duty_cycle=65535 if i < 2 else 0)
    gate = channel_gate.ChannelGate(4)
    for i, k in {0: "head_new", 1: "head_old", 2: "empty", 3: "empty"}.items():
        gate.set_kind(i, k)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, gate=gate,
                                             clock=bank.clock)
    r.set_target(bells[0], r.default_duty)

    class Tower:
        tower = "active"

    class Survey:
        phase = "idle"
        last_result = {}

    class Host:
        n = 4
    h = Host()
    h.bells, h.mux, h.tower, h.gate, h.refresher = bells, tower, Tower(), gate, r
    h.survey_state = Survey()
    h._prev_held = [True, False, False, False]
    h.pt_level = PtLevel(ConstADC(20000), 0, 27)
    h.tune = tune.Tuner(h, None, clock_ms=clock)
    h.tune.stuck.log = lambda s: None
    return h


class ConstADC:
    def __init__(self, v):
        self.value = v


@case
def test_tuner_lets_thr_and_drive_through_for_a_stuck_head_only():
    c = Clock()
    h = host(c)
    t = h.tune
    flicker(t.stuck, 0, c, AFTER_MS - 100)
    a = t.handle("tune", 0, "thr_step", 1)
    assert not a["ok"] and a["why"].startswith("tower not quiet"), a
    flicker(t.stuck, 0, c, 200)
    assert t.unlocked()
    a = t.handle("tune", 0, "thr_step", -1)
    assert a["ok"], a
    flicker(t.stuck, 0, c, t.settings["min_gap_ms"])
    a = t.handle("drive", 0, "drive_step", -1)
    assert a["ok"] and h.bells[0].laser.duty_cycle < 65535, a
    assert t.stuck.unlocks == 2
    # a survey still wins
    h.survey_state.phase = "examining"
    flicker(t.stuck, 0, c, t.settings["min_gap_ms"])
    a = t.handle("tune", 0, "thr_step", 1)
    assert not a["ok"] and a["why"] == "survey running", a
    h.survey_state.phase = "idle"
    # a real bell ringing on another channel shuts it again
    ring(t.stuck, 1, c, STROKE_EVERY_MS)
    flicker(t.stuck, 0, c, 10)
    assert not t.unlocked()
    a = t.handle("tune", 0, "thr_step", 1)
    assert not a["ok"] and a["why"].startswith("tower not quiet"), a
    # the tower quiet: the old path, and unlocked() is False (nothing to flag)
    h.tower.tower = "inactive"
    assert not t.unlocked()
    c.t += t.settings["min_gap_ms"]
    assert t.handle("tune", 0, "thr_step", 1)["ok"]


@case
def test_scope_flags_stuck_measures_and_sends_full_reports():
    c = Clock()
    h = host(c)
    sent = []

    def send_to(cid, obj):
        sent.append((cid, json.loads(json.dumps(obj))))
        return True
    al = A.Aligner(h, send_to, c, ("sandswing", "pico", "b00t"),
                   mem_free=lambda: 100000, log=lambda s: None)
    f = al._cc_flags(0)
    assert not f & heads_cc.FLAG_QUIET and not f & heads_cc.FLAG_STUCK
    flicker(h.tune.stuck, 0, c, AFTER_MS + 100)
    f = al._cc_flags(0)
    assert f & heads_cc.FLAG_STUCK and not f & heads_cc.FLAG_QUIET, f
    al.on_message(7, json.dumps({"cmd": "align", "ch": 1, "scope": True}))
    al.tick()
    flicker(h.tune.stuck, 0, c, A.FRAME_BUSY_MS)
    al.tick()
    frames = [o for cid, o in sent if o["type"] == "align"]
    assert frames and frames[-1]["body"]["stuck"] is True and frames[-1]["body"]["busy"] is False
    al.on_message(7, json.dumps({"cmd": "align", "ch": 1, "thr": -1}))
    al.tick()
    acks = [o["body"] for cid, o in sent if o["type"] == "align_ack" and o["body"]["what"] == "thr"]
    assert acks and acks[-1]["ok"] and acks[-1]["stuck"] is True, acks
    # save / sweep stay on the strict quiet gate
    assert al._busy_why() == "tower not quiet"


@case
def test_flag_fits_a_7_bit_cc_and_is_distinct():
    flags = (heads_cc.FLAG_CLEAR, heads_cc.FLAG_BROKE, heads_cc.FLAG_BUSY,
             heads_cc.FLAG_QUIET, heads_cc.FLAG_SCOPE, heads_cc.FLAG_STUCK)
    assert sum(flags) <= heads_cc.SEVEN and len(set(flags)) == len(flags)


@case
def test_files_txt_deploys_stuck_py():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = open(os.path.join(root, "tools", "files.txt")).read().split()
    assert "stuck.py" in names and "tower_watch.py" in names and "ring.py" in names


finish()
