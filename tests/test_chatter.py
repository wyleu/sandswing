# test_chatter.py -- the chatter counter (chatter.py): edges closer than
# chatter_gap_ms are one burst; an odd burst is a transition (1 edge clean),
# an even one is spurious edges in a steady state.  Counters only in the
# stroke path.  And the laser PWM limits (tune.py) and the align commands.
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import *  # noqa: E402
import chatter as C
import tune
import heads_cc as CC
import align_scope as A
from ring import MIN_STROKE_GAP_MS

GAP = C.CHATTER_GAP_MS
PASS_MS = 1000           # the vane: light this long, dark this long (sandtest, ~4 deg/s)
CHOP_MS = 1              # a chop edge each keypad scan (inputs.scan_interval_ms 1)


def run(w, i, edges):
    for t in edges:
        w.edge(i, t)


def clean_passes(t0, n):
    """n vane passes: one edge in, one edge out, PASS_MS apart."""
    out, t = [], t0
    for _ in range(n):
        out += [t, t + PASS_MS]
        t += 2 * PASS_MS
    return out


@case
def test_clean_passes_are_one_edge_per_transition():
    w = C.Chatter(2)
    w.start(0)
    run(w, 0, clean_passes(100, 5))
    w.stop(20000)
    assert w.summary(0, 20000) == (10, 10, 1, 10, 0, 0), w.summary(0, 20000)
    assert w.summary(1, 20000) == (0, 0, 0, None, 0, 0)


@case
def test_chatter_at_an_edge_is_one_transition_with_more_edges():
    w = C.Chatter(1)
    w.start(0)
    # 5 edges 2 ms apart (chop at the edge), then a clean edge back
    run(w, 0, [100, 102, 104, 106, 108, 100 + PASS_MS])
    w.stop(5000)
    tr, clean, mx, mean, burst, spur = w.summary(0, 5000)
    assert (tr, clean, mx, burst, spur) == (2, 1, 5, 8, 0), w.summary(0, 5000)
    assert mean == 30                                 # (5 + 1) / 2 = 3.0, x10


@case
def test_a_glitch_in_a_steady_state_is_spurious_not_a_transition():
    w = C.Chatter(1)
    w.start(0)
    run(w, 0, [100, 500, 501, 100 + PASS_MS])         # a 1 ms dip while lit
    w.stop(5000)
    assert w.summary(0, 5000) == (2, 2, 1, 10, 0, 2)


@case
def test_steady_flicker_is_one_long_burst():
    w = C.Chatter(1)
    w.start(0)
    run(w, 0, list(range(100, 100 + 500, CHOP_MS)))   # 500 edges: even, level unchanged
    w.stop(5000)
    tr, clean, mx, mean, burst, spur = w.summary(0, 5000)
    assert tr == 0 and spur == 500
    run(w, 0, [6000])                                 # stopped: not counted
    assert w.summary(0, 7000)[5] == 500


@case
def test_report_while_counting_settles_only_finished_bursts():
    w = C.Chatter(1)
    w.start(0)
    run(w, 0, [100, 105])
    assert w.summary(0, 100 + GAP - 1)[0] == 0 and w.b_n[0] == 2   # still open
    run(w, 0, [110])
    assert w.summary(0, 110 + GAP)[:3] == (1, 0, 3)
    w.start(200)                                      # start zeroes everything
    assert w.summary(0, 300) == (0, 0, 0, None, 0, 0) and w.on


@case
def test_gap_setting_limits():
    assert C.gap_setting(None) == GAP
    assert C.gap_setting({"tune": {"chatter_gap_ms": 5}}) == 5
    assert C.gap_setting({"tune": {"chatter_gap_ms": 0}}) == GAP
    assert C.gap_setting({"tune": {"chatter_gap_ms": MIN_STROKE_GAP_MS + 1}}) == GAP
    assert C.gap_setting(None, 7) == 7
    for bad in (0, -1, MIN_STROKE_GAP_MS + 1, True, 2.5, "5"):
        try:
            C.gap_setting(None, bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert C.CHATTER_GAP_MAX_MS == MIN_STROKE_GAP_MS
    # a real strip pass (two transitions ~60 ms apart) is never merged
    assert GAP < 60 // 2


@case
def test_edge_is_counters_only():
    """No call, list / dict / tuple building or string work in edge() (the
    stroke path): only compares, arithmetic and stores (and _close once a
    burst ends)."""
    src = open(os.path.join(SRC, "chatter.py")).read()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "edge"][0]
    for n in ast.walk(fn):
        assert not isinstance(n, (ast.List, ast.Dict, ast.Tuple, ast.JoinedStr, ast.ListComp)), \
            ast.unparse(n)
        if isinstance(n, ast.Call):
            assert ast.unparse(n.func) == "self._close", ast.unparse(n)
    cl = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_close"][0]
    assert not any(isinstance(n, ast.Call) for n in ast.walk(cl))


@case
def test_edge_allocates_nothing():
    import tracemalloc
    w = C.Chatter(8)
    w.start(0)
    tracemalloc.start()
    s0 = tracemalloc.take_snapshot()
    for k in range(250):
        w.edge(k & 7, k)          # CPython caches ints to 256 only (CircuitPython: 2**30)
    s1 = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grew = sum(d.size_diff for d in s1.compare_to(s0, "filename")
               if d.traceback[0].filename.endswith("chatter.py"))
    assert grew < 64, grew


@case
def test_pwm_limits_are_named_and_the_duty_keeps_1000_levels():
    assert tune.LASER_PWM_HZ_MIN == tune.MS_PER_S // tune.TUNE_BURST_MAX_MS == 200
    assert tune.LASER_PWM_HZ_MAX == tune.RP2350_CLK_HZ // tune.LASER_PWM_MIN_LEVELS == 150000
    # CircuitPython rp2: above clk/65536 TOP = clk / f - 1: clk / f duty levels
    assert tune.RP2350_CLK_HZ // tune.LASER_PWM_HZ_MAX >= tune.LASER_PWM_MIN_LEVELS
    # the finest drive nudge is still a whole number of steps at the top speed
    levels = tune.RP2350_CLK_HZ // tune.LASER_PWM_HZ_MAX
    assert tune.LASER_FINE_STEP * levels // tune.FULL >= 1
    for ok in (200, 1000, 20000, 150000):
        assert tune.pwm_hz_ok(ok)
    for bad in (199, 150001, 0, -1, True, 1000.0, "1000", None):
        assert not tune.pwm_hz_ok(bad)
    assert tune.laser_pwm_hz(None) == tune.LASER_HZ == 1000
    assert tune.laser_pwm_hz({"laser": {"pwm_hz": 20000}}) == 20000
    assert tune.laser_pwm_hz({"pins": {"laser_pwm_hz": 5000}}) == 5000
    assert tune.laser_pwm_hz({"laser": {"pwm_hz": 1}, "pins": {"laser_pwm_hz": 2000}}) == 2000
    # the 14-bit CC carries the top speed
    assert CC.hz14(tune.LASER_PWM_HZ_MAX) <= CC.MAX_14


@case
def test_align_parses_pwm_and_chatter():
    P = A.parse_command
    assert P('{"cmd":"align","ch":1,"pwm_hz":20000}', 4) == ("pwm", 0, 20000)
    assert P('{"cmd":"align","ch":2,"chatter":"start"}', 4) == ("chatter", 1, ("start", None))
    assert P('{"cmd":"align","ch":1,"chatter":"start","gap_ms":5}', 4) == (
        "chatter", 0, ("start", 5))
    assert P('{"cmd":"align","ch":1,"chatter":"stop"}', 4) == ("chatter", 0, ("stop", None))
    assert P('{"cmd":"align","ch":1,"chatter":"report"}', 4)[2][0] == "report"
    for bad, why in (('{"cmd":"align","ch":1,"pwm_hz":"fast"}', "bad pwm"),
                     ('{"cmd":"align","ch":1,"pwm_hz":1000.5}', "bad pwm"),
                     ('{"cmd":"align","ch":1,"chatter":"go"}', "bad chatter"),
                     ('{"cmd":"align","ch":1,"chatter":"stop","gap_ms":5}', "bad gap"),
                     ('{"cmd":"align","ch":1,"chatter":"start","gap_ms":"5"}', "bad gap"),
                     ('{"cmd":"align","ch":9,"chatter":"start"}', "bad channel")):
        try:
            P(bad, 4)
            raise AssertionError(bad)
        except ValueError as e:
            assert str(e) == why, (bad, e)
    assert A.CHATTER_OPS == tune.CHATTER_OPS


@case
def test_cc_numbers_are_free_and_in_the_undefined_ranges():
    seven = [CC.CC_HEAD, CC.CC_FLAGS, CC.CC_ACK_WHAT, CC.CC_ACK_WHY, CC.CC_SEQ, CC.CC_SWEEP,
             CC.CC_SLOT, CC.CC_CHATTER] + [CC.CC_LABEL + j for j in range(CC.LABEL_MAX)]
    fourteen = [CC.CC_THR, CC.CC_RB, CC.CC_DRV, CC.CC_MA, CC.CC_PT, CC.CC_PT_SPAN, CC.CC_PWM] \
        + list(CC.CHATTER_CCS)
    every = seven + fourteen + [c + CC.CC_LSB for c in fourteen]
    assert len(every) == len(set(every)), sorted(every)
    undefined = set([3, 9, 14, 15] + list(range(20, 32)) + list(range(102, 120)))
    assert set(seven + fourteen) <= undefined, set(seven + fourteen) - undefined
    assert set(c + CC.CC_LSB for c in fourteen) <= set(range(32, 64))
    assert CC.WHAT["pwm"] == 12 and CC.WHAT["chatter"] == 13
    assert CC.why_code("pwm out of range 200-150000 Hz") == 21
    assert CC.why_code("pwm not changed: pwm slice clash") == 22
    assert CC.why_code("no chatter test") == 23 and CC.why_code("bad gap") == 13


finish()
