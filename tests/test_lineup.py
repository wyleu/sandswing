# test_lineup.py -- head line-up: the drive sweep (sweep.py), "Use this",
# and the per-channel values saved in NVM (head_store.py) used at boot.
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import *  # noqa: E402
import channel_gate
import threshold_refresh
import tower_build
import tune
import heads_cc as CC
import head_store as HS
import align_scope as A
import sweep as SW
from pt_level import PtLevel

NVM_LEN = 4096                  # the RP2350's CircuitPython microcontroller.nvm


class CPNvm:
    """microcontroller.nvm stand-in: a fixed length, slice reads, slice
    writes of the same length only (CircuitPython's ByteArray); counts writes."""

    def __init__(self, n=NVM_LEN, fill=0xFF):
        self.b = bytearray([fill] * n)
        self.writes = 0

    def __len__(self):
        return len(self.b)

    def __getitem__(self, k):
        if not isinstance(k, slice):
            return self.b[k]
        return bytearray(self.b[k])

    def __setitem__(self, k, v):
        assert isinstance(k, slice)
        a, b, _ = k.indices(len(self.b))
        assert len(v) == b - a, "nvm: a slice write must keep the length"
        self.b[a:b] = bytes(v)
        self.writes += 1


class Clock:
    def __init__(self):
        self.t = 1000

    def __call__(self):
        return self.t


class LitADC:
    """The PT on GP27: rises with drive and flattens out (a plateau)."""

    def __init__(self, laser, top=40000, knee=8000):
        self.laser, self.top, self.knee = laser, top, knee

    @property
    def value(self):
        d = self.laser.duty_cycle
        return int(self.top * d / (d + self.knee))


class DimADC(LitADC):
    """Head 5's real PT (heads.pt_light "lowers"): ~2 V dark, falling with
    light to ~0.16 V on the reflector at full drive."""

    def __init__(self, laser, dark=40000, lit=3200, knee=8000):
        LitADC.__init__(self, laser, dark - lit, knee)
        self.dark = dark

    @property
    def value(self):
        return self.dark - LitADC.value.fget(self)


RAISES = {"heads": {"pt_light": "raises"}}   # the bench fake above: PT up with light


class MidiPort:
    def __init__(self):
        self.writes = []

    def write(self, buf):
        self.writes.append(bytes(buf))
        return len(buf)


def reports(port):
    """Every CC report as {cc: 7-bit value}."""
    out, cur = [], None
    for w in port.writes:
        for i in range(0, len(w), 3):
            st, cc, v = w[i], w[i + 1], w[i + 2]
            assert st == CC.CC_STATUS | (CC.CC_CHANNEL - 1) and cc < 120 and v < 128
            if cc == CC.CC_HEAD:
                cur = {}
            cur[cc] = v
            if cc == CC.CC_SEQ:
                out.append(cur)
    return out


def v14(r, cc):
    v = (r[cc] << 7) | r[cc + CC.CC_LSB]
    return None if v == CC.NONE_14 else v


def make(kinds=None, nvm=None, cc=False, cfg=None, lowers=False):
    """lowers: head 5 as measured (DimADC, held = light, default polarity);
    else the older fake (LitADC) with cfg heads.pt_light "raises"."""
    if cfg is None and not lowers:
        cfg = RAISES
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
    r.set_target(bells[0], 16000)            # 0.81 V: under the lit PT
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
    h._prev_held = [lowers, False, False, False]     # ch1 lit (the strip seen)
    h.pt_level = PtLevel((DimADC if lowers else LitADC)(bells[0].laser), 0, 27)
    h.head_store = HS.HeadStore(nvm if nvm is not None else CPNvm())
    h.tune = tune.Tuner(h, cfg, clock_ms=clock)
    sent = []

    def send_to(cid, obj):
        sent.append((cid, json.loads(json.dumps(obj))))
        return True
    h.midi = MidiPort()
    al = A.Aligner(h, send_to, clock, ("sandswing", "pico", "b00t"),
                   mem_free=lambda: 100000, log=lambda s: None,
                   cc=CC.CcOut(h.midi) if cc else None, cc_alive=(lambda: True) if cc else None)
    return h, al, sent, clock


def say(al, cid=7, **kw):
    d = {"cmd": "align"}
    d.update(kw)
    al.on_message(cid, json.dumps(d))
    al.tick()


def of(sent, typ):
    return [o["body"] for c, o in sent if o["type"] == typ]


def acks(sent, what):
    return [b for b in of(sent, A.T_ACK) if b["what"] == what]


def run_sweep(al, clock, limit=2000):
    k = 0
    while al.sweep is not None and k < limit:
        clock.t += int(SW.SWEEP_TICK_S * 1000)
        al.tick()
        k += 1
    return k


# ---- head_store ----------------------------------------------------------------
@case
def test_store_layout_fits_and_misses_the_watchdog_mark():
    assert HS.NVM_USED_B == HS.HDR_B + HS.SLOTS * HS.REC_B + HS.CRC_B <= HS.NVM_SIZE
    assert HS.NVM_OFFSET + HS.NVM_SIZE <= NVM_LEN - HS.WATCHDOG_MARK_B
    assert HS.LABEL_MAX == CC.LABEL_MAX
    assert HS.HeadStore(CPNvm(HS.NVM_SIZE)).state == "nvm too small"
    assert HS.HeadStore(None).state == "none"


@case
def test_store_blank_nvm_is_empty_and_save_round_trips():
    nvm = CPNvm()
    st = HS.HeadStore(nvm)
    assert st.state == "empty" and st.slots == [None] * HS.SLOTS and nvm.writes == 0
    assert st.save(2, 9000, 15000, "H5") is None
    assert nvm.writes == 1
    again = HS.HeadStore(nvm)                # the next boot
    assert again.state == "ok" and again.slots[2] == (9000, 15000, "H5")
    assert again.drive_map(4) == {2: 9000} and again.thr_map(4) == {2: 15000}
    assert again.drive_map(2) == {}          # a channel past n is not used
    assert again.save(0, 4000, None, "") is None    # an old head: no threshold
    assert HS.HeadStore(nvm).slots[0] == (4000, None, "")
    assert again.save(1, 65535, 65535, "12345678") is None   # full scale is a value
    assert HS.HeadStore(nvm).slots[1] == (65535, 65535, "12345678")
    assert again.forget(2) is None
    assert HS.HeadStore(nvm).slots[2] is None
    # the watchdog's 4-byte note at the end is never touched
    assert bytes(nvm.b[-4:]) == b"\xff" * 4


@case
def test_store_bad_checksum_version_or_magic_is_ignored():
    for off, label in ((HS.HDR_B, "checksum"), (2, "version"), (0, "magic")):
        nvm = CPNvm()
        HS.HeadStore(nvm).save(0, 9000, 15000, "A1")
        nvm.b[HS.NVM_OFFSET + off] ^= 0x01
        st = HS.HeadStore(nvm)
        assert st.slots == [None] * HS.SLOTS and st.state != "ok", (label, st.state)
        # a save writes a fresh image over it
        assert st.save(1, 5000, 12000, "B2") is None
        assert HS.HeadStore(nvm).slots[1] == (5000, 12000, "B2")
        assert HS.HeadStore(nvm).slots[0] is None


@case
def test_store_labels_are_checked():
    assert HS.check_label("12") == "12" and HS.check_label(None) == ""
    for bad in ("123456789", "a\"b", "é", "a\nb", 5):
        try:
            HS.check_label(bad)
        except ValueError:
            continue
        raise AssertionError("label %r accepted" % (bad,))
    st = HS.HeadStore(CPNvm())
    try:
        st.save(0, 9000, 1, "x" * (HS.LABEL_MAX + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("long label saved")
    assert st.save(0, 0, 1, "") == "laser is off"
    assert st.save(HS.SLOTS, 9000, 1, "") == "bad channel"


@case
def test_crc_is_ccitt_false():
    assert HS.crc16(b"123456789") == 0x29B1


@case
def test_nothing_else_uses_nvm():
    users = []
    for fn in sorted(os.listdir(SRC)):
        if fn.endswith(".py"):
            if ".nvm" in open(os.path.join(SRC, fn)).read():
                users.append(fn)
    assert users == ["head_store.py", "watchdog_guard.py"], users


# ---- boot: saved values replace the defaults --------------------------------------
@case
def test_boot_uses_saved_drive_and_threshold():
    nvm = CPNvm()
    HS.HeadStore(nvm).save(0, 9000, 21000, "H5")
    HS.HeadStore(nvm).save(1, 100, None, "old")    # under the floor: clamped
    h, al, sent, clock = make(nvm=nvm)
    h.tune.use_saved(h.head_store)
    assert h.tune.drive_map() == {0: 9000, 1: tune.LASER_MIN_DUTY}
    r = h.refresher
    r.target.clear()
    r.seed_kinds({0: "head_new", 1: "head_old"}, 32768, saved=h.tune.saved_thr)
    assert r.target == {0: 21000}
    r.target.clear()
    r.seed_kinds({0: "head_new"}, 32768)            # nothing saved: the default
    assert r.target == {0: 32768}


# ---- tune: floor and finer nudges near it --------------------------------------
@case
def test_floor_is_two_percent_and_nudges_are_finer_near_it():
    s = tune.laser_settings(None)
    assert s["min_duty"] == tune.LASER_MIN_DUTY < tune.LASER_FINE_STEP * 5
    assert tune.drive_step(30000, -1, s) == 30000 - tune.LASER_STEP
    assert tune.drive_step(tune.LASER_FINE_BELOW, -1, s) == tune.LASER_FINE_BELOW - tune.LASER_FINE_STEP
    assert tune.drive_step(5000, 1, s) == 5000 + tune.LASER_FINE_STEP
    assert tune.drive_step(tune.LASER_FINE_BELOW, 1, s) == tune.LASER_FINE_BELOW + tune.LASER_STEP
    h, al, sent, clock = make()
    t = h.tune
    assert t.handle("drive", 0, "drive", 1500, 1000)["ok"]
    a = t.handle("drive", 0, "drive_step", -1, 2000)
    assert a["ok"] and a["drv"] == tune.LASER_MIN_DUTY
    a = t.handle("drive", 0, "drive_step", -1, 3000)
    assert not a["ok"] and a["why"] == "at limit"


@case
def test_use_sets_drive_and_threshold_in_one_command():
    h, al, sent, clock = make()
    say(al, ch=1, use=[9000, 12000])
    a = acks(sent, "use")[-1]
    assert a["ok"] and a["drv"] == 9000 and a["thr"] == 12000
    assert h.bells[0].laser.duty_cycle == 9000 and h.refresher.target[0] == 12000
    clock.t += 1000
    say(al, ch=2, use=[9000, 12000])                 # old head: the drive only
    a = acks(sent, "use")[-1]
    assert a["ok"] and h.bells[1].laser.duty_cycle == 9000 and 1 not in h.refresher.target
    h.tower.tower = "active"
    clock.t += 1000
    say(al, ch=1, use=[8000, 12000])
    assert not acks(sent, "use")[-1]["ok"] and h.bells[0].laser.duty_cycle == 9000


# ---- the sweep -------------------------------------------------------------------
@case
def test_sweep_duties_descend_geometric_to_the_floor():
    d = SW.sweep_duties(65535, 1310, 16)
    assert d[0] == 65535 and d[-1] == 1310 and len(d) == 16
    assert all(a > b for a, b in zip(d, d[1:]))
    gaps = [a - b for a, b in zip(d, d[1:])]
    assert gaps[-1] < gaps[0] / 10              # finer near the floor
    assert SW.sweep_settings(None)["points"] == SW.SWEEP_POINTS
    s = SW.sweep_settings({"sweep": {"points": 500, "settle_ms": -1, "guard_frac": 2}})
    assert s == SW.sweep_settings(None)
    assert A.SWEEP_TICK_S == SW.SWEEP_TICK_S


@case
def test_sweep_reports_every_point_and_restores_the_drive():
    h, al, sent, clock = make()
    h.refresher.target[0] = 2000                 # far under the PT: no early stop
    h.bells[0].laser.duty_cycle = 30000
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["ok"] and h.tune.sweep_ch == 0
    assert al.period_s() == SW.SWEEP_TICK_S
    # a nudge while it runs is refused
    say(al, ch=1, drive=-1)
    assert acks(sent, "drive")[-1]["why"] == "sweep running"
    run_sweep(al, clock)
    pts = of(sent, A.T_SWEEP)
    assert [p["i"] for p in pts] == list(range(SW.SWEEP_POINTS))
    assert pts[0]["drv"] == 65535 and pts[-1]["drv"] == tune.LASER_MIN_DUTY
    assert pts[0]["pt"] > pts[-1]["pt"] > 0
    end = acks(sent, "sweep_end")[-1]
    assert end["ok"] and end["why"] is None
    assert h.bells[0].laser.duty_cycle == 30000 and h.tune.sweep_ch is None
    # every tick did at most one GP26 burst (no long blocking)
    assert h.tune.burst_us is None or h.tune.burst_us <= tune.TUNE_BURST_MAX_MS * 1000


@case
def test_sweep_ends_near_the_threshold():
    h, al, sent, clock = make()
    say(al, ch=1, sweep=True)
    run_sweep(al, clock)
    end = acks(sent, "sweep_end")[-1]
    assert end["why"] == SW.END_NEAR, end
    assert len(of(sent, A.T_SWEEP)) < SW.SWEEP_POINTS
    assert h.bells[0].laser.duty_cycle == 65535


@case
def test_sweep_aborts_and_restores_on_stroke_beam_stop_timeout():
    def start():
        h, al, sent, clock = make()
        h.refresher.target[0] = 2000
        h.bells[0].laser.duty_cycle = 40000
        say(al, ch=1, sweep=True)
        for _ in range(12):
            clock.t += 50
            al.tick()
        assert al.sweep is not None
        return h, al, sent, clock
    h, al, sent, clock = start()
    h.tower.tower = "active"
    clock.t += 50
    al.tick()
    assert acks(sent, "sweep_end")[-1]["why"] == "tower not quiet"
    assert h.bells[0].laser.duty_cycle == 40000 and al.sweep is None
    h, al, sent, clock = start()
    al.edge(0, True)                         # "raises": held = the light lost
    clock.t += 50
    al.tick()
    assert acks(sent, "sweep_end")[-1]["why"] == SW.END_DARK
    assert h.bells[0].laser.duty_cycle == 40000
    h, al, sent, clock = start()
    say(al, ch=1, sweep=False)
    assert acks(sent, "sweep_end")[-1]["why"] == "stopped"
    assert h.bells[0].laser.duty_cycle == 40000 and h.tune.sweep_ch is None
    h, al, sent, clock = start()
    clock.t += SW.SWEEP_TIMEOUT_S * 1000
    al.tick()
    assert acks(sent, "sweep_end")[-1]["why"] == "timeout"
    assert h.bells[0].laser.duty_cycle == 40000
    h, al, sent, clock = start()
    h.tune._measure = None                   # anything raising inside a step
    clock.t += 1000
    al.tick()
    assert al.sweep is None and h.bells[0].laser.duty_cycle == 40000
    assert acks(sent, "sweep_end")[-1]["why"] == "error"


@case
def test_sweep_refused_without_pt_wire_busy_or_broken():
    h, al, sent, clock = make()
    say(al, ch=2, sweep=True)
    assert acks(sent, "sweep")[-1]["why"].startswith("no PT wire")
    h.tower.tower = "active"
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["why"] == "tower not quiet"
    h.tower.tower = "inactive"
    h._prev_held[0] = True
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["why"] == SW.END_DARK
    h._prev_held[0] = False
    h.bells[0].laser.duty_cycle = 0
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["why"] == "laser is off"
    assert al.sweep is None and h.tune.sweep_ch is None
    assert CC.why_code("no PT wire on this channel") == 16
    assert CC.why_code("bad label") == 19 and CC.why_code("bad channel") == 13


# ---- save / forget / saved over the align protocol --------------------------------
@case
def test_save_forget_and_list_over_align():
    nvm = CPNvm()
    h, al, sent, clock = make(nvm=nvm)
    say(al, ch=1, use=[9000, 12000])
    say(al, ch=1, save=3, label="H5")
    a = acks(sent, "save")[-1]
    assert a["ok"], a
    assert HS.HeadStore(nvm).slots[2] == (9000, 12000, "H5") and nvm.writes == 1
    clock.t += 50
    al.tick()
    s = of(sent, A.T_SLOT)[-1]
    assert s["ch"] == 3 and s["saved"] and s["label"] == "H5" and s["drv"] == 9000
    say(al, saved=True)
    for _ in range(HS.SLOTS + 1):
        clock.t += 50
        al.tick()
    got = of(sent, A.T_SLOT)[-HS.SLOTS:]
    assert [g["ch"] for g in got] == list(range(1, HS.SLOTS + 1))
    assert [g["saved"] for g in got] == [False, False, True] + [False] * 5
    say(al, forget=3)
    assert acks(sent, "forget")[-1]["ok"] and HS.HeadStore(nvm).slots[2] is None
    # refused while ringing: no NVM write then
    h.tower.tower = "active"
    w = nvm.writes
    say(al, ch=1, save=1, label="X")
    assert acks(sent, "save")[-1]["why"] == "tower not quiet" and nvm.writes == w
    h.tower.tower = "inactive"
    say(al, ch=1, save=1, label='a"b')
    assert acks(sent, "bad")[-1]["why"] == "bad label" and nvm.writes == w
    h.head_store = None
    al.store = None
    say(al, ch=1, save=1)
    assert acks(sent, "save")[-1]["why"].startswith("not saved")


@case
def test_parse_lineup_commands():
    P = A.parse_command
    assert P('{"cmd":"align","ch":1,"sweep":true}', 8) == ("sweep", 0, True)
    assert P('{"cmd":"align","ch":1,"use":[9000,null]}', 8) == ("use", 0, (9000, None))
    assert P('{"cmd":"align","ch":1,"save":8,"label":"12"}', 8) == ("save", 0, (7, "12"))
    assert P('{"cmd":"align","forget":2}', 8) == ("forget", None, 1)
    assert P('{"cmd":"align","saved":true}', 8) == ("saved", None, True)
    for bad in ('{"cmd":"align","ch":1,"sweep":1}', '{"cmd":"align","ch":1,"use":[1]}',
                '{"cmd":"align","ch":1,"use":[70000,1]}', '{"cmd":"align","ch":1,"save":9}',
                '{"cmd":"align","ch":1,"label":"x"}', '{"cmd":"align","forget":0}',
                '{"cmd":"align","ch":1,"use":[true,1]}'):
        try:
            P(bad, 8)
        except ValueError:
            continue
        raise AssertionError("accepted %s" % bad)


# ---- the CC reports -----------------------------------------------------------------
@case
def test_cc_point_and_slot_reports_fit_and_decode():
    h, al, sent, clock = make(cc=True)
    h.refresher.target[0] = 2000
    say(al, cid=A.SERIAL_CID, ch=1, sweep=True)
    run_sweep(al, clock)
    rs = reports(h.midi)
    pts = [r for r in rs if CC.CC_SWEEP in r]
    assert len(pts) == SW.SWEEP_POINTS and all(len(r) <= CC.MAX_MSGS for r in rs)
    p0 = pts[0]
    assert p0[CC.CC_HEAD] == 1 and p0[CC.CC_SWEEP] == 0
    assert v14(p0, CC.CC_DRV) == CC.duty14(65535) and v14(p0, CC.CC_PT) > 0
    ends = [r for r in rs if r.get(CC.CC_ACK_WHAT) == CC.WHAT["sweep_end"]]
    assert ends and ends[-1][CC.CC_ACK_WHY] == 0
    say(al, cid=A.SERIAL_CID, ch=1, save=2, label="H5")
    clock.t += 50
    al.tick()
    sl = [r for r in reports(h.midi) if CC.CC_SLOT in r][-1]
    assert sl[CC.CC_HEAD] == 2 and sl[CC.CC_SLOT] == 1
    lab = "".join(chr(sl[CC.CC_LABEL + j]) for j in range(CC.LABEL_MAX) if sl[CC.CC_LABEL + j])
    assert lab == "H5"
    # a slot report is 15 messages, a point 12; numbers clear of each other
    used = [CC.CC_HEAD, CC.CC_FLAGS, CC.CC_ACK_WHAT, CC.CC_ACK_WHY, CC.CC_SEQ,
            CC.CC_SWEEP, CC.CC_SLOT] + [CC.CC_LABEL + j for j in range(CC.LABEL_MAX)]
    for c in (CC.CC_THR, CC.CC_RB, CC.CC_DRV, CC.CC_MA, CC.CC_PT, CC.CC_PT_SPAN):
        used += [c, c + CC.CC_LSB]
    assert len(used) == len(set(used)) and max(used) < 120


@case
def test_new_modules_are_lean_and_deployed():
    names = open(os.path.join(os.path.dirname(SRC), "tools", "files.txt")).read().split()
    assert "head_store.py" in names and "sweep.py" in names
    # sweep.py is imported only when a sweep is asked
    src = open(os.path.join(SRC, "align_scope.py")).read()
    top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any("sweep" in ast.unparse(n) for n in top)


# ---- heads.pt_light "lowers" (default; head 5: reflector 0.16 V, dark ~2 V) ----
@case
def test_polarity_setting_default_lowers():
    from pt_level import light_lowers
    assert light_lowers(None) and light_lowers({}) and light_lowers({"heads": {"pt_light": "x"}})
    assert not light_lowers(RAISES)
    h, al, sent, clock = make(lowers=True)
    assert h.tune.light_lowers and h.tune.lit(True) and not h.tune.lit(False)
    assert al._cc_flags(0) & CC.FLAG_LIT_LOW
    h2, al2, _, _ = make()
    assert not al2._cc_flags(0) & CC.FLAG_LIT_LOW


@case
def test_lowers_sweep_runs_with_the_strip_seen_held():
    h, al, sent, clock = make(lowers=True)
    h.refresher.target[0] = 64000                # ~3.2 V: far above the dark PT, no early stop
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["ok"], acks(sent, "sweep")
    run_sweep(al, clock)
    pts = of(sent, A.T_SWEEP)
    assert len(pts) == SW.SWEEP_POINTS
    assert pts[0]["pt"] < pts[-1]["pt"]          # less light: the PT rises
    assert acks(sent, "sweep_end")[-1]["why"] is None


@case
def test_lowers_sweep_refused_and_aborted_when_the_light_goes():
    h, al, sent, clock = make(lowers=True)
    h._prev_held[0] = False                      # comparator high: no light
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["why"] == SW.END_DARK
    h._prev_held[0] = True
    h.refresher.target[0] = 64000
    say(al, ch=1, sweep=True)
    assert acks(sent, "sweep")[-1]["ok"]
    al.edge(0, True)                             # held = light: not a loss
    clock.t += 50
    al.tick()
    assert al.sweep is not None
    al.edge(0, False)                            # released: the light lost
    clock.t += 50
    al.tick()
    assert acks(sent, "sweep_end")[-1]["why"] == SW.END_DARK
    assert h.bells[0].laser.duty_cycle == 65535


@case
def test_lowers_sweep_stops_before_the_pt_rises_to_the_threshold():
    h, al, sent, clock = make(lowers=True)
    h.refresher.target[0] = 32768                # 1.65 V: mid-rail, as head 5 now
    say(al, ch=1, sweep=True)
    run_sweep(al, clock)
    end = acks(sent, "sweep_end")[-1]
    assert end["why"] == SW.END_NEAR, end
    pts = of(sent, A.T_SWEEP)
    tv = 32768 * 3.3 / 65535
    assert all(p["pt"] < tv for p in pts), pts   # never past the threshold
    assert len(pts) < SW.SWEEP_POINTS


finish()
