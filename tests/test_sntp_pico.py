# Plain-CPython tests for sntp_pico.py with fake socketpool/supervisor.
#   python3 test_sntp_pico.py
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import sntp_pico as S

UTC0 = 1792310400000.0          # Unix ms at world time 0 (2026-10-18 ...)


class World:
    """True time, a Pico tick clock with offset/drift, and an NTP server."""
    def __init__(self, tick0=5000, drift_ppm=0.0, stratum=1, li=0, proc_ms=0.2):
        self.t = 0.0              # true ms since world start
        self.tick0 = tick0
        self.drift = drift_ppm * 1e-6
        self.stratum, self.li, self.proc = stratum, li, proc_ms
        self.delays = []          # per query: (up_ms, down_ms) | None (timeout) | "boom"
        self.default = (2.0, 2.0)
        self.sent = 0
        self.bias = 0.0           # server error (ms): lets tests move the "truth"

    def ticks(self):
        return int(self.tick0 + self.t * (1.0 + self.drift)) & S.TICKS_MASK

    def utc(self):
        return UTC0 + self.t + self.bias

    def sleep(self, s):
        self.t += s * 1000.0

    def true_offset(self):
        """utc - extended tick, ignoring quantisation (drift 0 case)."""
        return UTC0 - self.tick0


def ntp_stamp(unix_ms):
    sec = int(unix_ms // 1000) + S.NTP_UNIX_DELTA_S
    frac = int((unix_ms % 1000) / 1000.0 * 4294967296.0) & 0xFFFFFFFF
    return struct.pack(">II", sec, frac)


class FakeSock:
    def __init__(self, world):
        self.w = world
        self.timeout = None
        self.reply = None
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, pkt, addr):
        w = self.w
        w.sent += 1
        d = w.delays.pop(0) if w.delays else w.default
        site = getattr(w, "per_ip", {}).get(addr[0])   # several servers
        if site is not None:
            d = None if site.get("dead") else site.get("delay", d)
        if d is None:
            w.t += self.timeout * 1000.0
            self.reply = None
            return 48
        if d == "boom":
            raise RuntimeError("radio fell over")
        up, down = d
        w.t += up
        t2 = w.utc() + (site or {}).get("bias", 0.0)
        t3 = t2 + w.proc
        r = bytearray(48)
        r[0] = (w.li << 6) | (4 << 3) | 4
        r[1] = (site or {}).get("stratum", w.stratum)
        r[24:32] = pkt[40:48]
        r[32:40] = ntp_stamp(t2)
        r[40:48] = ntp_stamp(t3)
        w.t += w.proc + down
        self.reply = r
        return 48

    def recvfrom_into(self, buf):
        if self.reply is None:
            raise OSError(116, "ETIMEDOUT")
        buf[:48] = self.reply
        return 48, ("10.0.0.5", 123)

    def close(self):
        self.closed = True


class FakePool:
    AF_INET = 2
    SOCK_DGRAM = 2

    def __init__(self, world):
        self.w = world

    def socket(self, fam, typ):
        return FakeSock(self.w)


def make(world, **kw):
    clock = S.TickClock(world.ticks)
    kw.setdefault("gap_s", 0.05)
    return S.SntpClock(FakePool(world), ["10.0.0.5"], clock=clock,
                       sleep_fn=world.sleep, **kw), clock


results = []


def case(fn):
    try:
        fn()
        results.append(True)
        print("  ok  ", fn.__name__)
    except Exception as e:
        results.append(False)
        print("  FAIL", fn.__name__, repr(e))
    return fn


@case
def test_offset_math_known_values():
    off, rtt = S.offset_delay(1000, 1500, 1501, 1011)
    assert off == 495.0 and rtt == 10, (off, rtt)
    assert S.ntp_to_unix_ms(S.NTP_UNIX_DELTA_S + 1, 1 << 31) == 1500.0
    w = World()
    c, clk = make(w, burst=4)
    assert c.burst_once()
    t = clk.now()
    assert abs(c.utc_ms(t) - (UTC0 + w.t)) <= 1.5, (c.utc_ms(t), UTC0 + w.t)
    assert c.state() == "synced"
    e = c.error_ms(t)
    assert 1.0 <= e <= 4.0, e                     # rtt/2 + tick + resid


@case
def test_min_rtt_sample_is_used():
    w = World()
    c, clk = make(w, burst=5)
    # asymmetric (biased) samples with big RTT, one clean 2 ms one
    w.delays = [(30.0, 2.0), (2.0, 25.0), (1.0, 1.0), (15.0, 3.0), (9.0, 9.0)]
    assert c.burst_once()
    assert c.last["rtt_ms"] < 3.0, c.last
    assert abs(c.last["offset_ms"] - w.true_offset()) <= 1.5, c.last


@case
def test_reject_stratum_0_16_and_unsynced_leap():
    for st, li in ((0, 0), (16, 0), (2, 3)):
        w = World(stratum=st, li=li)
        c, clk = make(w, burst=3)
        assert not c.burst_once()
        assert c.state() == "unsynced"
        assert c.utc_ms(clk.now()) is None and c.error_ms(clk.now()) is None
        assert c.stats["rejected_stratum"] == 3, c.stats


@case
def test_reject_outlier_rtt_from_history():
    w = World()
    c, clk = make(w, burst=2, interval_s=300)
    for d in (3.0, 4.0, 3.5, 4.5, 3.0):
        w.default = (d / 2, d / 2)
        assert c.burst_once()
        w.sleep(300)
    n = len(c.samples)
    w.default = (40.0, 40.0)                      # congested Wi-Fi: rtt ~80
    assert not c.burst_once()
    assert c.stats["rejected_rtt"] == 1 and len(c.samples) == n
    assert "rtt" in c.last["why"]


@case
def test_rtt_gate_relearns_after_a_lasting_path_change():
    """A lasting RTT step (busy hotspot) must not hold the clock in holdover
    for ever: after RTT_RELEARN_AFTER rejections in a row it is accepted."""
    w = World()
    c, clk = make(w, burst=2, interval_s=300)
    for d in (3.0, 4.0, 3.5, 4.5, 3.0):
        w.default = (d / 2, d / 2)
        assert c.burst_once()
        w.sleep(300)
    w.default = (40.0, 40.0)
    got = []
    for _ in range(S.RTT_RELEARN_AFTER):
        got.append(c.burst_once())
        w.sleep(300)
    assert got == [False] * (S.RTT_RELEARN_AFTER - 1) + [True], got
    assert c.stats["rejected_rtt"] == S.RTT_RELEARN_AFTER - 1
    assert c.state() == "synced" and c.last["ok"]
    assert c.burst_once()                         # the new path is now normal


@case
def test_unsynchronised_server_is_named_and_holdover_kept():
    """sandmon in GPS NO Lock without local/orphan: replies with stratum 16
    / leap 3.  The Pico keeps holdover (still stamps utc) and says why."""
    w = World()
    c, clk = make(w, burst=3, interval_s=300, stale_after=3)
    assert c.burst_once() and c.state() == "synced"
    w.stratum, w.li = 16, 3
    for _ in range(4):
        w.sleep(300)
        assert not c.burst_once()
    assert "not synchronised" in c.last["why"] and "16" in c.last["why"], c.last
    assert c.peer == {"srv": "10.0.0.5", "stratum": 16, "leap": 3}, c.peer
    assert c.state() == "stale"
    st = c.stamp()
    assert st["utc_ms"] is not None and st["sync"] == "stale"   # strokes keep utc
    assert c.boot_record()["peer"]["stratum"] == 16
    w.stratum, w.li = 1, 0                        # lock back
    w.sleep(300)
    assert c.burst_once() and c.state() == "synced"


@case
def test_no_reply_still_says_no_reply():
    w = World()
    c, clk = make(w, burst=2)
    w.default = None
    assert not c.burst_once() and c.last["why"] == "no reply"


@case
def test_drift_fit_predicts_ahead():
    w = World(drift_ppm=40.0)
    c, clk = make(w, burst=3, interval_s=300, fit_samples=8)
    for _ in range(6):
        assert c.burst_once()
        w.sleep(300)
    assert abs(c.b * 1e6 + 40.0) < 3.0, c.b * 1e6   # offset slope ~ -drift
    w.sleep(600)                                   # 10 min after the last burst
    t = clk.now()
    err = abs(c.utc_ms(t) - (UTC0 + w.t))
    assert err <= 2.0, err
    assert c.error_ms(t) >= err                    # bound holds
    # without the fit, 40 ppm over 15 min would be ~36 ms
    assert 40e-6 * (900 * 1000) > 30


@case
def test_ticks_wrap_extension():
    raw = {"v": S.TICKS_MASK - 100}
    clk = S.TickClock(lambda: raw["v"])
    e0 = clk.now()
    raw["v"] = 199                                  # wrapped, +300 ms
    e1 = clk.now()
    assert e1 - e0 == 300 and clk.wraps == 1, (e1 - e0, clk.wraps)
    older = clk.extend(S.TICKS_MASK - 50)           # stamped just before wrap
    assert older == e0 + 50, (older, e0)
    # sync across a wrap stays consistent
    w = World(tick0=S.TICKS_MASK - 400000)          # wraps ~400 s in
    c, clk = make(w, burst=2, interval_s=300)
    for _ in range(4):
        assert c.burst_once()
        w.sleep(300)
    assert clk.wraps == 1
    t = clk.now()
    assert abs(c.utc_ms(t) - (UTC0 + w.t)) <= 2.0


@case
def test_stale_after_missed_syncs():
    w = World()
    c, clk = make(w, burst=2, interval_s=300, stale_after=3)
    assert c.burst_once() and c.state() == "synced"
    e_fresh = c.error_ms(clk.now())
    for _ in range(3):
        w.delays = [None, None]
        w.sleep(300)
        assert not c.burst_once()
    w.sleep(1)
    assert c.state() == "stale", c.state()
    t = clk.now()
    assert c.utc_ms(t) is not None                  # still usable, flagged
    assert c.error_ms(t) > e_fresh
    assert c.stamp()["sync"] == "stale"


@case
def test_unsynced_stamp_has_null_utc():
    w = World()
    c, clk = make(w)
    st = c.stamp()
    assert st["utc_ms"] is None and st["err_ms"] is None
    assert st["sync"] == "unsynced" and isinstance(st["tick_ms"], int)
    assert c.boot_record()["sync"] == "unsynced"


@case
def test_stale_reply_with_wrong_originate_ignored():
    w = World()
    c, clk = make(w, burst=1)
    orig = FakeSock.sendto

    def bad(self, pkt, addr):
        p = bytearray(pkt)
        p[40:48] = b"\x00" * 8                      # reply to someone else
        return orig(self, p, addr)
    FakeSock.sendto = bad
    try:
        assert not c.burst_once()
    finally:
        FakeSock.sendto = orig
    assert c.stats["bad"] == 1 and c.state() == "unsynced"


@case
def test_timeout_and_errors_never_leave_run_loop():
    w = World()
    w.delays = [None, "boom"] * 50
    c, clk = make(w, burst=2, interval_s=0.001, gap_s=0.0)

    async def go():
        try:
            await asyncio.wait_for(c.run(), 0.05)
        except asyncio.TimeoutError:
            return True                             # loop still alive
        return False
    assert asyncio.run(go())
    assert c.stats["timeout"] >= 1 and c.stats["bad"] >= 1, c.stats
    assert c.state() == "unsynced"
    # each query is bounded by the socket timeout
    assert c.timeout_s <= 0.25


@case
def test_accuracy_classes_and_holdover():
    w = World(drift_ppm=20.0)
    c, clk = make(w, burst=3, interval_s=300, stale_after=3)
    assert c.accuracy_class(clk.now()) is None          # unsynced
    for _ in range(4):
        assert c.burst_once()
        w.sleep(300)
    assert c.accuracy_class(clk.now()) == "ms"
    # GPS lost: holdover on the last offset/drift; the bound grows, class decays
    w.delays = [None] * 1000
    w.sleep(6 * 3600)
    t = clk.now()
    assert c.state() == "stale" and c.utc_ms(t) is not None
    err = abs(c.utc_ms(t) - (UTC0 + w.t))
    assert err <= c.error_ms(t), (err, c.error_ms(t))   # bound still honest
    assert c.error_ms(t) >= 6 * 3600 * 1000 * 5e-6       # hold_ppm floor
    assert c.accuracy_class(t) == "s"                    # tower clock: fine
    c.drift_unc = 50e-6                                  # worst-case crystal
    assert c.accuracy_class(t) is None                   # 6 h x 50 ppm > 0.5 s


@case
def test_second_server_used_when_first_silent():
    w = World()
    clock = S.TickClock(w.ticks)
    c = S.SntpClock(FakePool(w), ["10.0.0.5", "10.0.0.6"], clock=clock,
                    burst=1, sleep_fn=w.sleep)
    w.delays = [None, (1.0, 1.0)]
    assert c.burst_once() and c.stats["timeout"] == 1


def synced(w, **kw):
    kw.setdefault("burst", 3)
    c, clk = make(w, **kw)
    for _ in range(4):
        assert c.burst_once()
        w.sleep(300)
    return c, clk


@case
def test_ringing_freezes_mapping_despite_new_offsets():
    w = World(drift_ppm=10.0)
    c, clk = synced(w)
    c.set_ringing(True)
    assert c.mode() == "ringing" and c.state() == "held"
    t0 = clk.now()
    seg = c.segs[-1]
    ep, sess = c.epoch, c.session
    # seg = (t0, u0, dr): u0 + d + round(d * dr), d = ticks since t0
    pred = lambda t: seg[1] + (t - seg[0]) + int(round((t - seg[0]) * seg[2]))
    stamps = []
    for k, bias in enumerate([+40.0, -60.0, +500.0, -5.0]):
        w.bias = bias                       # server now disagrees
        w.delays = [None] * 2 if k == 1 else []   # lock lost then regained
        c.burst_once()                      # diagnostic sample, never applied
        w.sleep(600)
        st = c.stamp()
        stamps.append(st)
        assert st["utc_ms"] == pred(st["tick_ms"]), "mapping moved"
        assert st["epoch"] == ep and st["session"] == sess
        assert st["mode"] == "ringing"
    # rate-constant: utc differences equal tick differences times one rate
    for a, b in zip(stamps, stamps[1:]):
        du, dt = b["utc_ms"] - a["utc_ms"], b["tick_ms"] - a["tick_ms"]
        assert abs(du - dt * (1.0 + seg[2])) <= 1, (du, dt)
    assert c.guard_hits == 0


def _end_session(bias):
    w = World(drift_ppm=0.0)
    c, clk = synced(w, step_ms=100.0, max_slew_ppm=500.0)
    c.set_ringing(True)
    w.sleep(1800)
    w.bias = bias
    for _ in range(3):                      # estimate learns the new truth
        c.burst_once(); w.sleep(60)
    last = c.stamp()
    c.set_ringing(False)
    out = [last]
    for _ in range(400):                    # step 1 s through the slew
        w.sleep(1.0)
        out.append(c.stamp())
    for a, b in zip(out, out[1:]):
        assert b["utc_ms"] >= a["utc_ms"], (a, b)
    return c, clk, w, out


@case
def test_session_end_small_positive_slews():
    c, clk, w, out = _end_session(+50.0)          # 50 ms <= step_ms -> slew
    ev = c.events[-1]
    assert ev["kind"] == "slew" and 45.0 <= ev["correction_ms"] <= 55.0, ev
    assert abs(ev["slew_s"] - ev["correction_ms"] / 0.5) < 1e-6  # ms / 500 ppm
    t = out[-1]["tick_ms"]                        # slew done: on the estimate
    assert abs(out[-1]["utc_ms"] - c._est(t)) <= 1
    assert abs(out[-1]["utc_ms"] - (UTC0 + w.t + 50.0)) <= c.error_ms(t)


@case
def test_session_end_negative_slews_never_backwards():
    c, clk, w, out = _end_session(-80.0)
    ev = c.events[-1]
    assert ev["kind"] == "slew" and -85.0 <= ev["correction_ms"] <= -75.0, ev
    # ~80 ms at 500 ppm = ~160 s; then on the estimate
    t = out[-1]["tick_ms"]
    assert abs(out[-1]["utc_ms"] - c._est(t)) <= 1
    assert abs(out[-1]["utc_ms"] - (UTC0 + w.t - 80.0)) <= c.error_ms(t)
    # during the slew each 1 s step still advances (>= 999.5 ms)
    assert min(b["utc_ms"] - a["utc_ms"] for a, b in zip(out, out[1:])) >= 998


@case
def test_session_end_large_positive_steps_forward():
    c, clk, w, out = _end_session(+400.0)
    kinds = [e["kind"] for e in c.events]
    assert "step" in kinds
    st = [e for e in c.events if e["kind"] == "step"][-1]
    assert st["epoch"] == out[1]["epoch"] > out[0]["epoch"]
    assert out[1]["utc_ms"] - out[0]["utc_ms"] >= 400


@case
def test_free_mode_corrections_never_go_backwards():
    w = World(drift_ppm=0.0)
    c, clk = synced(w)
    prev = c.stamp()
    for bias in [-30.0, +20.0, -200.0, -1.0]:
        w.bias = bias
        c.burst_once()
        for _ in range(20):
            w.sleep(5.0)
            st = c.stamp()
            assert st["utc_ms"] >= prev["utc_ms"]
            prev = st


@case
def test_ring_started_unsynced_stays_null():
    w = World()
    c, clk = make(w, burst=3)
    c.set_ringing(True)
    assert c.stamp()["utc_ms"] is None
    assert c.burst_once()                    # sync arrives mid-session
    w.sleep(10)
    st = c.stamp()
    assert st["utc_ms"] is None and st["tick_ms"] is not None
    assert st["err_ms"] is None and st["acc"] is None
    c.set_ringing(False)
    assert c.stamp()["utc_ms"] is not None   # appears only after session end


@case
def test_four_hour_drift_bound():
    wc = S.SntpClock.worst_case_ms
    assert abs(wc(4, 50.0) - 720.0) < 1e-9      # crystal spec, uncorrected
    assert abs(wc(4, 5.0) - 72.0) < 1e-9        # fitted drift, hold_ppm floor
    w = World(drift_ppm=30.0)
    c, clk = synced(w)
    c.set_ringing(True)
    t0 = clk.now()
    e0 = c.error_ms(t0)
    w.delays = [None] * 1000                    # GPS gone for the whole ring
    w.sleep(4 * 3600)
    t = clk.now()
    growth = c.error_ms(t) - e0
    assert growth >= 72.0 - 1e-6, growth        # >= 4 h x 5 ppm floor
    err = abs(c.utc_ms(t) - (UTC0 + w.t))
    assert err <= c.error_ms(t), (err, c.error_ms(t))
    assert c.accuracy_class(t) == "s"           # absolute: ms -> s class
    # relative timing: a 2 s stroke interval from tick deltas is unaffected
    a = c.stamp(); w.sleep(2.0); b = c.stamp()
    assert abs((b["utc_ms"] - a["utc_ms"]) - 2000 * (1 + 30e-6)) <= 1.5


# ---- integer time (CircuitPython floats are 32-bit) ----------------------
BENCH_UTC_MS = 1792575275008    # 2026-10-21 09:34:35.008, the bench sync time
RUN_S = 60                      # simulated run of stamps
STEP_S = 1.0                    # one stamp per second
RUN_TOL_MS = 3                  # utc advance over RUN_S vs true elapsed


def f32(x):
    """x as a CircuitPython (IEEE single) float would hold it."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def bench_world():
    w = World(tick0=9110071)             # tick_ms seen on the bench
    w.t = BENCH_UTC_MS - UTC0            # true time = the bench sync time
    return w


@case
def test_ntp_parse_is_integer_ms():
    sec, frac = struct.unpack(">II", ntp_stamp(BENCH_UTC_MS + 0.5))
    ms = S.ntp_to_unix_ms(sec, frac)
    assert isinstance(ms, int) and abs(ms - BENCH_UTC_MS) <= 1, ms
    us = S.ntp_to_unix_us(sec, frac)
    assert isinstance(us, int) and abs(us - (BENCH_UTC_MS * 1000 + 500)) <= 1, us
    # why: the bench value is itself a float32 step (131072 ms); 1 ms later
    # and 60 s later still read back as the same float32
    assert BENCH_UTC_MS % (1 << 17) == 0
    assert f32(BENCH_UTC_MS + 1) == f32(BENCH_UTC_MS + RUN_S * 1000) == BENCH_UTC_MS


@case
def test_utc_int_and_advances_60s_with_2026_base():
    w = bench_world()
    c, clk = make(w, burst=4)
    assert c.burst_once()
    first = c.stamp()
    prev = first
    for _ in range(int(RUN_S / STEP_S)):
        w.sleep(STEP_S)
        st = c.stamp()
        assert isinstance(st["utc_ms"], int) and isinstance(st["tick_ms"], int), st
        assert st["utc_ms"] > prev["utc_ms"], (prev, st)
        prev = st
    adv = prev["utc_ms"] - first["utc_ms"]
    assert abs(adv - RUN_S * 1000) <= RUN_TOL_MS, adv
    assert abs(prev["utc_ms"] - (UTC0 + w.t)) <= RUN_TOL_MS
    e = prev["err_ms"]
    assert 0 < e < RUN_TOL_MS * 10, e                # small, not ~2**20


@case
def test_stamps_1ms_apart_differ():
    w = bench_world()
    c, clk = make(w, burst=4)
    assert c.burst_once()
    a = c.stamp()
    w.sleep(0.001)
    b = c.stamp()
    assert b["tick_ms"] - a["tick_ms"] == 1, (a, b)
    assert b["utc_ms"] - a["utc_ms"] == 1, (a, b)


@case
def test_float32_state_keeps_ms():
    """Only small floats are kept: rounding them to float32 (as the Pico
    would) does not move any stamp; the big bases are ints."""
    w = World(tick0=9110071, drift_ppm=30.0)
    w.t = BENCH_UTC_MS - UTC0
    c, clk = synced(w)
    assert isinstance(c.a, int) and all(isinstance(sg[0], int) and isinstance(sg[1], int)
                                        for sg in c.segs), (c.a, c.segs)
    ticks = [clk.now() + k * int(STEP_S * 1000) for k in range(RUN_S)]
    want = [c.utc_ms(t) for t in ticks]
    c.a_frac, c.b = f32(c.a_frac), f32(c.b)
    c.segs = [(t0, u0, f32(dr)) for t0, u0, dr in c.segs]
    got = [c.utc_ms(t) for t in ticks]
    assert max(abs(g - x) for g, x in zip(got, want)) <= 1, (got[-1], want[-1])
    assert all(isinstance(e, int) for e in got)


# ---- stamp rate (bench: -2700 ppm with a -56 ppm crystal) ----------------
BENCH_DRIFT_PPM = -56.0         # /status last.drift_ppm after the fix's parent
RATE_RUN_S = 30                 # stamp run like the bench stroke set
RATE_TOL_PPM = 100              # utc must track ticks*(1+drift) within this


@case
def test_short_span_fit_keeps_b_zero():
    """Two boot bursts ~1 s apart must not invent a ~2000 ppm slope from
    tick-quantisation / RTT-asymmetry noise (the bench -2700 ppm case)."""
    w = World(tick0=9110071, drift_ppm=0.0)
    w.t = BENCH_UTC_MS - UTC0
    c, clk = make(w, burst=8, interval_s=1.0, fit_samples=16)
    assert c.burst_once()
    w.bias = 2.7                                 # ~2.7 ms noise over ~1 s
    w.sleep(1.0)                                 # as close as two run() bursts
    assert c.burst_once()
    assert c.b == 0.0, c.b * 1e6                 # unfixed would be ~+2700 ppm
    assert abs(c.last["drift_ppm"]) < 1e-9
    # then ring for 30 s on that mapping: utc tracks ticks (true drift 0)
    first = c.stamp()
    c.set_ringing(True)
    for _ in range(RATE_RUN_S):
        w.sleep(1.0)
    last = c.stamp()
    dt = last["tick_ms"] - first["tick_ms"]
    ppm = ((last["utc_ms"] - first["utc_ms"]) / float(dt) - 1.0) * 1e6
    assert abs(ppm) <= RATE_TOL_PPM, ppm


@case
def test_stamp_rate_matches_ticks_over_30s():
    """A -56 ppm crystal: over 30 s of stamps (free then held), utc advances
    within RATE_TOL_PPM of ticks * (1 + drift).  No continuous slew."""
    w = World(tick0=9110071, drift_ppm=-BENCH_DRIFT_PPM)  # World sign: Pico slow
    w.t = BENCH_UTC_MS - UTC0
    c, clk = make(w, burst=8, interval_s=300, fit_samples=8)
    for _ in range(6):                           # span long enough to fit
        assert c.burst_once()
        w.sleep(300)
    assert abs(c.b * 1e6 - BENCH_DRIFT_PPM) < 5.0, c.b * 1e6
    first = c.stamp()
    c.set_ringing(True)
    held = c.stamp()
    for _ in range(RATE_RUN_S):
        w.sleep(1.0)
        c.stamp()
    last = c.stamp()
    dt = last["tick_ms"] - first["tick_ms"]
    du = last["utc_ms"] - first["utc_ms"]
    ppm = (du / float(dt) - 1.0) * 1e6
    assert abs(ppm - BENCH_DRIFT_PPM) <= RATE_TOL_PPM, (ppm, BENCH_DRIFT_PPM, dt, du)
    # free -> held first step also stays near 1:1 (bench: 1064 vs 1061)
    d0 = held["tick_ms"] - first["tick_ms"]
    if d0:
        assert abs((held["utc_ms"] - first["utc_ms"]) / float(d0) - 1.0) * 1e6 <= RATE_TOL_PPM


@case
def test_accuracy_class_buckets_named():
    assert S.ACC_MS_MAX_MS == 10.0 and S.ACC_S_MAX_MS == 500.0
    w = World()
    c, clk = synced(w)
    t = clk.now()
    # the rule: e <= ACC_MS_MAX_MS -> "ms", else <= ACC_S_MAX_MS -> "s"
    for err, want in ((5.2, "ms"), (10.5, "s")):   # 5.2 = the bench flip
        c.error_ms = lambda tick, e=err: e
        assert c.accuracy_class(t) == want, (err, c.accuracy_class(t))

# ---- time servers: subnet filter, choice, switching ----------------------
import time_servers as TS
import time_select as TSel

HOME_GPS, TOWER_GPS = "192.168.0.221", "10.42.0.1"
NTP_IPS = [HOME_GPS, TOWER_GPS]
LAN_MASK = "255.255.255.0"
A_IP, B_IP = "10.0.0.5", "10.0.0.6"
SLOW_RTT = (4.0, 4.0)
FAST_RTT = (1.0, 1.0)


def two_servers(a, b, **kw):
    """World with servers A_IP / B_IP configured by dicts (stratum, delay,
    bias, dead)."""
    w = World()
    w.per_ip = {A_IP: a, B_IP: b}
    clock = S.TickClock(w.ticks)
    kw.setdefault("burst", 3)
    c = S.SntpClock(FakePool(w), [A_IP, B_IP], clock=clock, sleep_fn=w.sleep, **kw)
    return w, c, clock


@case
def test_subnet_filter_home_and_tower():
    assert TS.candidates(NTP_IPS, "192.168.0.81", LAN_MASK) == [HOME_GPS]
    assert TS.candidates(NTP_IPS, "10.42.0.23", LAN_MASK) == [TOWER_GPS]
    logged = []
    assert TS.candidates(NTP_IPS, "172.16.5.9", LAN_MASK, log=logged.append) == NTP_IPS
    assert logged and "trying all" in logged[0], logged
    assert TS.candidates(NTP_IPS, None, None) == NTP_IPS


@case
def test_string_ntp_ips_is_one_ip_not_chars():
    assert TS.normalise(HOME_GPS) == [HOME_GPS]
    assert TS.normalise(HOME_GPS + ", " + TOWER_GPS) == NTP_IPS
    w = World()
    c = S.SntpClock(FakePool(w), HOME_GPS, clock=S.TickClock(w.ticks))
    assert c.servers == [HOME_GPS], c.servers
    assert TS.ip_to_int("256.1.1.1") is None and TS.ip_to_int("1.2.3") is None


@case
def test_select_lower_stratum_then_rtt_and_disagree():
    r = lambda ip, st, rtt, off: {"ip": ip, "stratum": st, "rtt_ms": rtt, "offset_ms": off}
    best, dis = TSel.choose([r("a", 2, 1.0, 0), r("b", 1, 9.0, 0)])
    assert best["ip"] == "b" and not dis               # stratum beats RTT
    best, dis = TSel.choose([r("a", 1, 5.0, 0), r("b", 1, 2.0, 1)])
    assert best["ip"] == "b"                           # tie: lowest RTT
    best, dis = TSel.choose([r("a", 1, 2.0, 0), r("b", 1, 3.0, TSel.SERVER_DISAGREE_MS + 1)])
    assert dis
    best, dis = TSel.choose([r("a", 0, 1.0, 0), r("b", 16, 1.0, 0), r("c", None, None, None)])
    assert best is None and not dis


@case
def test_clock_bursts_only_the_chosen_server():
    w, c, clk = two_servers({"stratum": 2, "delay": FAST_RTT}, {"stratum": 1, "delay": SLOW_RTT})
    assert c.burst_once()
    assert c.server == B_IP and c.stratum == 1 and c.last["server"] == B_IP
    st = c.stamp()
    assert st["srv"] == B_IP and st["stratum"] == 1
    br = c.boot_record()
    assert br["srv"] == B_IP and br["stratum"] == 1 and br["disagree"] is False


@case
def test_server_switch_new_epoch_clean_fit():
    a, b = {"stratum": 1, "delay": FAST_RTT}, {"stratum": 1, "delay": SLOW_RTT}
    w, c, clk = two_servers(a, b, interval_s=300)
    for _ in range(3):
        assert c.burst_once()
        w.sleep(300)
    assert c.server == A_IP and len(c.samples) == 3
    ep = c.epoch
    a["dead"] = True                                   # home box gone
    assert c.burst_once()
    assert c.server == B_IP and len(c.samples) == 1, (c.server, c.samples)
    assert c.epoch > ep and any(e["kind"] == "server" for e in c.events)
    assert c.stamp()["srv"] == B_IP


@case
def test_disagree_flag_from_probe():
    w, c, clk = two_servers({"stratum": 1, "delay": FAST_RTT},
                            {"stratum": 1, "delay": SLOW_RTT,
                             "bias": TSel.SERVER_DISAGREE_MS * 2.0})
    assert c.burst_once() and c.disagree and c.boot_record()["disagree"]


@case
def test_no_server_switch_while_ringing():
    a, b = {"stratum": 1, "delay": FAST_RTT}, {"stratum": 2, "delay": FAST_RTT}
    w, c, clk = two_servers(a, b)
    assert c.burst_once() and c.server == A_IP
    c.set_ringing(True)
    ep = c.epoch
    a["stratum"], b["stratum"] = 3, 1                  # B now better
    c.burst_once()                                     # diagnostic only
    assert c.server == A_IP and c.epoch == ep
    c.set_ringing(False)
    assert c.burst_once() and c.server == B_IP         # reselect when free


@case
def test_unsynced_retries_fast():
    w = World()
    w.default = None                                   # nothing answers
    c, clk = make(w, burst=2, interval_s=300)
    assert not c.burst_once() and c.state() == "unsynced"
    assert c._wait_s(False) == S.BOOT_RETRY_S < c.interval_s
    w.default = FAST_RTT
    assert c.burst_once()
    assert c._wait_s(False) == c.interval_s


@case
def test_every_stamp_carries_srv_and_stratum():
    w = World()
    c, clk = make(w, burst=2)
    st = c.stamp()
    assert "srv" in st and st["srv"] is None and st["stratum"] is None   # unsynced
    assert c.burst_once()
    for _ in range(3):
        w.sleep(1.0)
        st = c.stamp()
        assert st["srv"] == A_IP and st["stratum"] == w.stratum, st
    import sand_event
    ev = sand_event.Emitter(src="sandswing").make(c, "stroke", {"ch": 1})
    assert ev["srv"] == A_IP and ev["stratum"] == w.stratum


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
sys.exit(1 if n_fail else 0)
