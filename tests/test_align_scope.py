# test_align_scope.py -- the sandheads page's scope (align.js protocol)
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import *  # noqa: E402
import channel_gate
import threshold_refresh
import tower_build
import tune
import align_scope as A
from pt_level import PtLevel


class Clock:
    def __init__(self):
        self.t = 1000

    def __call__(self):
        return self.t


class ConstADC:
    def __init__(self, v):
        self.value = v


def make(kinds=None, mem=100000):
    """Host like test_tune.make_host plus what Aligner reads; ch1 new head
    with a PT wire on GP27 (here a constant ADC)."""
    mux_select.reset_registry()
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    for i, b in enumerate(bells):
        b.laser = bank.pwm_out(8 + i, frequency=1000, duty_cycle=65535 if i < 2 else 0)
    gate = channel_gate.ChannelGate(4)
    kinds = kinds or {0: "head_new", 1: "head_old", 2: "empty", 3: "trigger"}
    for i, k in kinds.items():
        gate.set_kind(i, k)
    r = threshold_refresh.ThresholdRefresher(tower, bells[0].drive, bells, gate=gate,
                                             clock=bank.clock)
    clock = Clock()

    class Tower:
        tower = "inactive"

    class Survey:
        phase = "idle"
        last_result = {}

    class Host:
        n = 4
    h = Host()
    h.bells, h.mux, h.tower, h.gate, h.refresher = bells, tower, Tower(), gate, r
    h.survey_state = Survey()
    h._prev_held = [False] * 4
    h.pt_level = PtLevel(ConstADC(20000), 0, 27)
    h.tune = tune.Tuner(h, None, clock_ms=clock)
    sent = []

    def send_to(cid, obj):
        sent.append((cid, json.loads(json.dumps(obj))))
        return True
    al = A.Aligner(h, send_to, clock, ("sandswing", "pico", "b00t"),
                   mem_free=lambda: mem, log=lambda s: None)
    return h, al, sent, clock


def say(al, cid=7, **kw):
    d = {"cmd": "align"}
    d.update(kw)
    al.on_message(cid, json.dumps(d))
    al.tick()


def of(sent, typ):
    return [o for c, o in sent if o["type"] == typ]


@case
def test_ring_window_and_bits_round_trip():
    r = A.SampleRing(8)
    for k, bit in enumerate([1, 1, 0, 1, 0, 0, 1, 1, 1, 0]):
        r.add(bit, 100 + 20 * k)
    n, dt, gap = r.window()
    assert n == 8 and dt == 20 and gap == 20, (n, dt, gap)
    assert A.unpack_bits(r.hex_bits(8), 8) == [0, 1, 0, 0, 1, 1, 1, 0]


@case
def test_ring_window_keeps_only_the_last_window_and_survives_the_tick_wrap():
    r = A.SampleRing(16)
    t0 = A.MASK29 - 30
    for k in range(4):
        r.add(1, t0 + 20 * k)                  # crosses 2**29
    assert r.window(1000)[0] == 4
    r.add(0, t0 + 5000)
    assert r.window(1000)[0] == 1


@case
def test_parse_matches_align_js_frames_and_refuses_the_rest():
    P = A.parse_command
    assert P('{"cmd":"align","info":true}', 8) == ("info", None, True)
    assert P('{"cmd":"align","ch":2,"scope":true}', 8) == ("scope", 1, True)
    assert P('{"cmd":"align","ch":2,"scope":false}', 8) == ("scope", 1, False)
    assert P('{"cmd":"align","ch":1,"thr":-1}', 8) == ("thr", 0, -1)
    assert P('{"cmd":"align","ch":8,"drive":1}', 8) == ("drive", 7, 1)
    for bad in ('{', '[]', '{"cmd":"tune"}', '{"cmd":"align","ch":0,"thr":1}',
                '{"cmd":"align","ch":9,"thr":1}', '{"cmd":"align","ch":1,"thr":0}',
                '{"cmd":"align","ch":1,"thr":true}', '{"cmd":"align","ch":1}',
                '{"cmd":"align","ch":1,"thr":1,"x":2}'):
        try:
            P(bad, 8)
        except ValueError:
            continue
        raise AssertionError(bad)


@case
def test_info_lists_kinds_active_heads_limits_and_the_pt_wire():
    h, al, sent, clock = make()
    say(al, info=True)
    (info,) = of(sent, "align_info")
    b = info["body"]
    assert info["schema"] == "sand.panel/v1" and sent[0][0] == 7
    assert b["kinds"] == ["head_new", "head_old", "empty", "trigger"]
    assert b["active"] == [1, 2] and b["scope"] is None and b["tower"] == "inactive"
    assert b["step"] == {"thr": h.tune.settings["threshold"]["step"],
                         "drv": h.tune.settings["laser"]["step"]}
    assert b["pt"]["ch"] == 1 and b["pt"]["gp"] == 27


@case
def test_nothing_runs_until_a_page_asks():
    h, al, sent, clock = make()
    al.sample()
    al.tick()
    assert al.ring.count == 0 and not sent and al.period_s() == A.IDLE_S


@case
def test_scope_frames_carry_what_align_js_reads():
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    (ack,) = of(sent, "align_ack")
    assert ack["body"]["what"] == "scope" and ack["body"]["ok"] and ack["body"]["ch"] == 1
    for k in range(10):
        clock.t += 20
        h._prev_held[0] = 3 <= k < 5           # beam broken for two passes
        al.sample()
    clock.t += A.FRAME_MS
    al.tick()
    fr = of(sent, "align")[-1]                 # (the first went out at the start)
    b = fr["body"]
    for key in ("ch", "n", "dt", "gap", "bits", "ma", "rb", "thr", "drv", "tower",
                "busy", "pt", "pt_span"):
        assert key in b, key
    assert b["ch"] == 1 and b["n"] == 10 and b["dt"] == 20.0 and b["tower"] == "inactive"
    assert A.unpack_bits(b["bits"], b["n"]) == [1, 1, 1, 0, 0, 1, 1, 1, 1, 1]
    assert b["drv"] == 65535 and b["busy"] is False and isinstance(b["ma"], float)
    assert b["pt"] == round(20000 * 3.3 / 65535, 3) and b["pt_span"] == 0.0
    assert "seq" not in fr                    # never mistaken for a stroke seq by farm_ws


@case
def test_a_beam_break_shorter_than_a_pass_still_shows():
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    al.sample()
    al.edge(0, True)                          # down and back up inside one pass
    al.edge(0, False)
    al.edge(1, True)                          # another channel: ignored
    al.sample()
    al.sample()
    assert A.unpack_bits(al.ring.hex_bits(3), 3) == [1, 0, 1]


@case
def test_pt_is_null_on_a_channel_without_the_wire():
    h, al, sent, clock = make()
    say(al, ch=2, scope=True)
    clock.t += A.FRAME_MS
    al.sample()
    al.tick()
    b = of(sent, "align")[-1]["body"]
    assert b["ch"] == 2 and b["pt"] is None and b["pt_span"] is None


@case
def test_inactive_heads_are_refused_with_the_kind():
    h, al, sent, clock = make()
    say(al, ch=3, scope=True)
    say(al, ch=4, thr=1)
    a1, a2 = of(sent, "align_ack")
    assert not a1["body"]["ok"] and a1["body"]["why"] == "not an active head (empty)"
    assert not a2["body"]["ok"] and "not an active head (trigger)" == a2["body"]["why"]
    assert al.ch is None


@case
def test_nudges_go_through_the_tuner_and_ack_the_new_values():
    h, al, sent, clock = make()
    say(al, ch=1, thr=1)
    a = of(sent, "align_ack")[-1]["body"]
    step = h.tune.settings["threshold"]["step"]
    assert a["what"] == "thr" and a["ok"] and a["thr"] == h.refresher.default_duty + step
    clock.t += 300
    say(al, ch=1, drive=-1)
    a = of(sent, "align_ack")[-1]["body"]
    assert a["what"] == "drive" and a["ok"] and a["drv"] == 65535 - h.tune.settings["laser"]["step"]
    assert h.tune.drive_map() == {0: a["drv"]}            # kept for a re-survey (RAM)
    say(al, ch=1, drive=1)                                # inside min gap
    a = of(sent, "align_ack")[-1]["body"]
    assert not a["ok"] and a["why"] == "too fast"
    clock.t += 300
    say(al, ch=2, thr=1)
    assert of(sent, "align_ack")[-1]["body"]["why"] == "old head has no threshold"


@case
def test_while_strokes_are_in_progress_nothing_is_adjusted_or_measured():
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    h.tower.tower = "active"
    say(al, ch=1, thr=1)
    a = of(sent, "align_ack")[-1]["body"]
    assert not a["ok"] and a["why"].startswith("tower not quiet") and a["tower"] == "active"
    measured = []
    h.tune._measure = lambda ch: measured.append(ch)
    clock.t += A.FRAME_BUSY_MS
    al.sample()
    al.tick()
    b = of(sent, "align")[-1]["body"]
    assert b["busy"] is True and not measured
    assert al.period_s() == A.FRAME_BUSY_MS / 1000


@case
def test_measure_is_rate_limited():
    h, al, sent, clock = make()
    n = []
    real = h.tune._measure
    h.tune._measure = lambda ch: (n.append(ch), real(ch))[1]
    say(al, ch=1, scope=True)
    for _ in range(10):
        clock.t += A.FRAME_MS
        al.sample()
        al.tick()
    frames = len(of(sent, "align"))
    assert frames == 11, frames               # one at the start, then one per FRAME_MS
    assert len(n) == 1 + (10 * A.FRAME_MS) // A.MEASURE_MS, len(n)


@case
def test_scope_stops_by_itself_timeout_head_gone_low_memory_client_gone():
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    clock.t += A.KEEPALIVE_MS + 1
    al.tick()
    assert al.ch is None and of(sent, "align_ack")[-1]["body"]["why"] == "timeout"
    say(al, ch=1, scope=True)
    h.gate.kind[0] = "empty"
    al.tick()
    assert al.ch is None and of(sent, "align_ack")[-1]["body"]["why"] == "head gone"
    h, al, sent, clock = make(mem=A.MEM_MIN - 1)
    say(al, ch=1, scope=True)
    assert al.ch is None and of(sent, "align_ack")[-1]["body"]["why"] == "low memory"
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    al.send_to = lambda cid, obj: None
    clock.t += A.FRAME_MS
    al.tick()
    assert al.ch is None


@case
def test_keepalive_and_switching_heads():
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    n = len(of(sent, "align_ack"))
    clock.t += 2000
    say(al, ch=1, scope=True)                 # keep-alive: no ack, still on
    assert len(of(sent, "align_ack")) == n and al.ch == 0
    clock.t += A.KEEPALIVE_MS - 1000
    al.tick()
    assert al.ch == 0
    say(al, ch=1, scope=False)
    assert al.ch is None
    say(al, ch=2, scope=True, cid=9)
    assert al.ch == 1 and al.cid == 9


@case
def test_bad_frames_ack_and_flooding_is_bounded():
    h, al, sent, clock = make()
    say(al, ch=1, thr="x")
    assert of(sent, "align_ack")[-1]["body"] == {
        "what": "bad", "ch": None, "ok": False, "why": "bad step", "thr": None,
        "drv": None, "pwm_hz": h.tune.pwm_hz(), "tower": "inactive", "stuck": False,
        "lit_low": True}
    for _ in range(A.MAX_PENDING + 3):
        al.on_message(1, '{"cmd":"align","info":true}')
    assert len(al.pending) == A.MAX_PENDING and al.dropped == 3
    al.on_message(1, "x" * (A.MSG_MAX_B + 1))
    assert al.errors == 1


@case
def test_errors_never_escape_and_switch_the_scope_off_after_max():
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    h._prev_held = None                       # sample() will fail
    for _ in range(A.MAX_ERRORS):
        al.sample()
    assert al.dead and al.ch is None
    al.tick()
    al.on_message(1, '{"cmd":"align","info":true}')
    assert not al.pending


@case
def test_no_settings_are_written():
    h, al, sent, clock = make()
    assert not hasattr(h.tune, "save")
    src = open(os.path.join(SRC, "align_scope.py")).read()
    assert "open(" not in src and "settings.json\"" not in src


@case
def test_sample_allocates_nothing_per_call():
    import tracemalloc
    h, al, sent, clock = make()
    say(al, ch=1, scope=True)
    for _ in range(200):
        al.sample()
    tracemalloc.start()
    s1 = tracemalloc.take_snapshot()
    for _ in range(500):
        al.sample()
    s2 = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grew = sum(d.size_diff for d in s2.compare_to(s1, "filename")
               if d.traceback[0].filename.endswith("align_scope.py"))
    assert grew < 100, grew                   # nothing kept per call (500 calls)

@case
def test_chatter_over_ws_is_a_json_report():
    h, al, sent, clock = make()
    say(al, ch=1, chatter="start")
    assert of(sent, "align_ack")[-1]["body"]["what"] == "chatter"
    for t in (2000, 2001, 2002, 3000):
        h.tune.chatter.edge(0, t)
    clock.t = 4000
    say(al, ch=1, chatter="report")
    b = of(sent, "align_chatter")[-1]["body"]
    assert b == {"ch": 1, "on": True, "trans": 2, "clean": 1, "max_edges": 3, "mean_x10": 20,
                 "max_burst_ms": 2, "spurious": 0, "gap_ms": 20, "pwm_hz": 1000}, b


finish()
