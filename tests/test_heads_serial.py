# test_heads_serial.py -- the sandheads scope over the USB data port
# (ring_send "off": no Wi-Fi, Sandbells' ingest_serial relays the page),
# the heartbeat's PT level, and one line at a time on the port.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import *  # noqa: E402
import channel_gate
import mux_select
import threshold_refresh
import tower_build
import tune
import serial_status
import align_scope as A
import pt_level
import heads_cc as CC
from pt_level import PtLevel


class Clock:
    def __init__(self):
        self.t = 1000

    def __call__(self):
        return self.t


class ConstADC:
    def __init__(self, v):
        self.value = v
        self.reads = 0

    def __getattribute__(self, k):
        if k == "value":
            object.__setattr__(self, "reads", object.__getattribute__(self, "reads") + 1)
        return object.__getattribute__(self, k)


class Port:
    """usb_cdc.data stand-in: bounded writes, injectable inbound."""

    def __init__(self, room=100000, connected=True):
        self.room, self.connected = room, connected
        self.out = bytearray()
        self._in = bytearray()
        self.timeout = None
        self.write_timeout = None

    def feed(self, data):
        self._in += data.encode("utf-8") if isinstance(data, str) else data

    @property
    def in_waiting(self):
        return len(self._in)

    def read(self, n):
        n = min(n, len(self._in))
        out = bytes(self._in[:n])
        del self._in[:n]
        return out

    def write(self, buf):
        n = min(len(buf), self.room)
        self.out += bytes(buf[:n])
        return n

    def lines(self):
        return [json.loads(l) for l in bytes(self.out).split(b"\n") if l.strip()]


class MidiPort:
    """usb_midi PortOut stand-in: records each write."""

    def __init__(self):
        self.writes = []

    def write(self, buf):
        self.writes.append(bytes(buf))
        return len(buf)


def decode(data):
    """CC bytes -> list of reports {cc: value} (14-bit pairs joined)."""
    reports, cur = [], None
    for i in range(0, len(data), 3):
        st, cc, v = data[i], data[i + 1], data[i + 2]
        assert st == CC.CC_STATUS | (CC.CC_CHANNEL - 1) and cc < 128 and v < 128, data[i:i + 3]
        if cc == CC.CC_HEAD:
            cur = {"raw": {}}
        cur["raw"][cc] = v
        if cc == CC.CC_SEQ:
            r = cur["raw"]
            out = {k: r[k] for k in (CC.CC_HEAD, CC.CC_FLAGS, CC.CC_ACK_WHAT, CC.CC_ACK_WHY, CC.CC_SEQ)
                   if k in r}
            for k in (CC.CC_THR, CC.CC_RB, CC.CC_DRV, CC.CC_MA, CC.CC_PT, CC.CC_PT_SPAN):
                if k in r:
                    out[k] = (r[k] << 7) | r[k + CC.CC_LSB]
            reports.append(out)
            cur = None
    return reports


def last_ack(h):
    for w in reversed(h.midi.writes):
        for r in reversed(decode(w)):
            if CC.CC_ACK_WHAT in r:
                return r
    return None


def make(kinds=None, cc=False):
    """A host like test_align_scope.make, wired the way Concentrator does
    it for the USB data port: SerialTune -> Aligner(SERIAL_CID) -> send."""
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
    h.adc = ConstADC(20000)
    h.pt_level = PtLevel(h.adc, 0, 27)
    h.tune = tune.Tuner(h, None, clock_ms=clock)
    port = Port()
    st = tune.SerialTune(port, h.tune)

    def send_to(cid, obj):
        assert cid == A.SERIAL_CID
        return st.send(obj)
    h.midi = MidiPort() if cc else None
    al = A.Aligner(h, send_to, clock, ("sandswing", "pico", "b00t"),
                   mem_free=lambda: 100000, log=lambda s: None,
                   cc=CC.CcOut(h.midi) if cc else None,
                   cc_alive=(lambda: port.connected) if cc else None)
    st.align_rx = lambda text: al.on_message(A.SERIAL_CID, text)
    return h, al, st, port, clock


def say(st, al, port, clock, **kw):
    d = {"cmd": "align"}
    d.update(kw)
    port.feed(json.dumps(d) + "\n")
    st.poll(clock.t / 1000.0)
    al.tick()


def of(port, typ):
    return [o for o in port.lines() if o.get("type") == typ]


@case
def test_align_lines_reach_the_scope_and_frames_come_back_on_the_port():
    h, al, st, port, clock = make()
    say(st, al, port, clock, info=True)
    info = of(port, A.T_INFO)
    assert info and info[-1]["body"]["active"] == [1, 2], info
    say(st, al, port, clock, ch=1, scope=True)
    acks = of(port, A.T_ACK)
    assert acks[-1]["body"]["what"] == "scope" and acks[-1]["body"]["ok"]
    frames = of(port, A.T_FRAME)
    b = frames[-1]["body"]
    assert b["ch"] == 1 and b["ma"] is not None and b["pt"] is not None
    assert b["drv"] == 65535 and info[-1]["body"]["frame_ms"] == A.SERIAL_FRAME_MS
    assert st.aligned == 2 and st.bad == 0 and st.lines == 0     # not tune commands


@case
def test_serial_frames_are_slower_than_ws_frames():
    h, al, st, port, clock = make()
    say(st, al, port, clock, ch=1, scope=True)
    assert al.period_s() == A.SERIAL_FRAME_MS / 1000.0
    n0 = len(of(port, A.T_FRAME))
    step, span = A.FRAME_MS // 2, 10 * A.FRAME_MS
    for _ in range(span // step):
        clock.t += step
        al.sample()
        al.tick()
        st.poll(clock.t / 1000.0)
    got = len(of(port, A.T_FRAME)) - n0
    assert got == span // A.SERIAL_FRAME_MS, got
    h.tower.tower = "active"
    assert al.period_s() == A.SERIAL_FRAME_BUSY_MS / 1000.0
    assert A.SERIAL_FRAME_MS >= A.FRAME_MS and A.SERIAL_FRAME_BUSY_MS >= A.FRAME_BUSY_MS


@case
def test_keepalive_timeout_stops_the_scope_when_the_host_goes_quiet():
    h, al, st, port, clock = make()
    say(st, al, port, clock, ch=1, scope=True)
    clock.t += A.KEEPALIVE_MS // 2
    say(st, al, port, clock, ch=1, scope=True)        # keep-alive
    assert al.ch == 0
    clock.t += A.KEEPALIVE_MS + 1
    al.tick()
    st.poll(clock.t / 1000.0)
    assert al.ch is None
    last = of(port, A.T_ACK)[-1]["body"]
    assert last["what"] == "scope" and last["ok"] is False and last["why"] == "timeout"


@case
def test_port_closed_by_the_host_stops_the_scope_at_once():
    h, al, st, port, clock = make()
    say(st, al, port, clock, ch=1, scope=True)
    port.connected = False                    # ingest_serial stopped / unplugged
    clock.t += A.SERIAL_FRAME_MS
    al.tick()
    assert al.ch is None
    assert st.send({"x": 1}) is None


@case
def test_nudges_over_serial_go_through_the_tuner_and_stay_refused_while_ringing():
    h, al, st, port, clock = make()
    say(st, al, port, clock, ch=1, scope=True)
    before = h.refresher.target.get(0)
    clock.t += 1000
    say(st, al, port, clock, ch=1, thr=1)
    a = of(port, A.T_ACK)[-1]["body"]
    assert a["what"] == "thr" and a["ok"], a
    assert h.refresher.target[0] != before
    h.tower.tower = "active"
    clock.t += 1000
    say(st, al, port, clock, ch=1, drive=-1)
    a = of(port, A.T_ACK)[-1]["body"]
    assert a["what"] == "drive" and not a["ok"] and a["why"].startswith("tower not quiet")
    assert h.bells[0].laser.duty_cycle == 65535


@case
def test_bad_align_lines_are_acked_bad_and_tune_lines_still_work():
    h, al, st, port, clock = make()
    for bad in ('{"cmd":"align","ch":9,"scope":true}', '{"cmd":"align","nope":1}',
                '{"cmd":"align","ch":1,"thr":"up"}', '{"cmd":"align"'):
        port.feed(bad + "\n")
        st.poll(clock.t / 1000.0)
        al.tick()
    bad_acks = [o for o in of(port, A.T_ACK) if o["body"]["what"] == "bad"]
    assert len(bad_acks) == 4, port.lines()
    port.feed('{"cmd":"tune_get","ch":1}\n')
    st.poll(clock.t / 1000.0)
    assert [o for o in port.lines() if o.get("t") == "tune"]
    assert al.ch is None


@case
def test_while_ringing_the_frame_still_reads_the_pt_level_but_not_the_mux():
    h, al, st, port, clock = make()
    say(st, al, port, clock, ch=1, scope=True)
    measured = []
    h.tune._measure = lambda ch: measured.append(ch)
    h.tower.tower = "active"
    reads = h.adc.reads
    h.adc.value = 30000
    clock.t += A.SERIAL_FRAME_BUSY_MS
    al.sample()
    al.tick()
    st.poll(clock.t / 1000.0)
    b = of(port, A.T_FRAME)[-1]["body"]
    assert b["busy"] is True and not measured
    assert h.adc.reads - reads == pt_level.PT_N
    assert b["pt"] == pt_level.counts_to_v(30000), b


@case
def test_a_part_written_line_is_finished_before_a_newer_one_starts():
    h, al, st, port, clock = make()
    port.room = 10
    st.send({"a": "x" * 40})
    assert st.busy() and st._off == 10
    st.send({"b": 1})                       # waits behind it
    st.send({"c": 2})                       # replaces the waiting one
    assert st.dropped_ack == 1
    port.room = 100000
    st.poll(1.0)
    st.poll(1.05)
    lines = port.lines()
    assert lines == [{"a": "x" * 40}, {"c": 2}], lines
    assert not st.busy()


@case
def test_a_held_line_not_yet_started_is_replaced_by_the_newest():
    h, al, st, port, clock = make()
    st.other_busy = lambda: True             # SerialStatus mid-line
    st.send({"a": 1})
    st.send({"b": 2})
    assert st.dropped_ack == 1 and port.out == b""
    st.poll(1.0, busy=True)
    assert port.out == b""
    st.poll(1.05, busy=False)
    assert port.lines() == [{"b": 2}]


@case
def test_serial_status_starts_nothing_while_another_writer_is_mid_line():
    port = Port()
    built = []

    def build():
        built.append(1)
        return {"schema": "sand.panel/v1", "type": "heartbeat", "body": {}}
    ss = serial_status.SerialStatus(port, build, period_s=10.0, now=0.0, stamp={})
    w = ss.step(0.0, hold=True)
    assert w == serial_status.SERIAL_PUSH_S and not built and port.out == b""
    ss.step(0.05, hold=False)
    assert built and ss.lines == 1


@case
def test_heartbeat_build_error_sends_a_short_serr_line_and_logs_it():
    port = Port()
    said = []

    def build():
        raise MemoryError("memory allocation failed, allocating 296 bytes")
    ss = serial_status.SerialStatus(port, build, period_s=10.0, now=0.0, stamp={},
                                    log=said.append)
    ss.step(0.0)
    line = port.lines()[-1]
    assert line["t"] == serial_status.SERIAL_ERR_TYPE and line["n"] == 1
    assert "MemoryError" in line["e"] and ss.errors == 1
    assert len(said) == 1 and "not built" in said[0]
    for k in range(2, serial_status.SERIAL_ERR_LOG_EVERY + 1):
        ss.step(10.0 * k)
    assert len(said) == 2, said              # first, then every SERIAL_ERR_LOG_EVERY
    assert len(bytes(port.out)) < serial_status.SERIAL_ERR_LOG_EVERY * 120


@case
def test_pt_brief_only_for_an_active_wired_head():
    pt = PtLevel(ConstADC(31000), 0, 27)
    assert pt_level.brief(None) is None
    got = pt_level.brief(pt, lambda c: "head_new")
    assert got == [1, pt_level.counts_to_v(31000), 0.0], got
    assert pt_level.brief(pt, lambda c: "empty") is None
    assert pt_level.PT_ACTIVE_KINDS == tune.ACTIVE_KINDS == A.ACTIVE_KINDS
    assert "pt" in serial_status.SERIAL_BODY_EXTRA


@case
def test_sample_and_edge_still_allocate_nothing_with_the_serial_scope():
    import tracemalloc
    h, al, st, port, clock = make()
    say(st, al, port, clock, ch=1, scope=True)
    tracemalloc.start()
    snap0 = tracemalloc.take_snapshot()
    for i in range(500):
        al.edge(0, i & 1)
        al.sample()
    snap1 = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grew = sum(d.size_diff for d in snap1.compare_to(snap0, "filename")
               if d.traceback[0].filename.endswith("align_scope.py"))
    assert grew < 100, grew



@case
def test_cc_scope_ack_and_frame_reports_carry_the_readings_and_no_json():
    h, al, st, port, clock = make(cc=True)
    h.refresher.set_target(h.bells[0], 40000)
    say(st, al, port, clock, ch=1, scope=True)
    assert port.out == b""                              # nothing back on the data port
    assert len(h.midi.writes) == 2                     # ack, then the first frame
    for w in h.midi.writes:
        assert len(w) % 3 == 0 and len(w) <= 3 * CC.MAX_MSGS
    ack = decode(h.midi.writes[0])
    assert len(ack) == 1, ack
    a = ack[0]
    assert a[CC.CC_HEAD] == 1 and a[CC.CC_ACK_WHAT] == CC.WHAT["scope"] and a[CC.CC_ACK_WHY] == 0
    fr = decode(h.midi.writes[1])[0]
    assert fr[CC.CC_HEAD] == 1 and CC.CC_ACK_WHAT not in fr
    assert fr[CC.CC_THR] == CC.duty14(40000) and fr[CC.CC_DRV] == CC.MAX_14
    assert fr[CC.CC_MA] == int(h.tune.ma[0] * CC.MA_UNITS + 0.5)
    assert fr[CC.CC_PT] == int(pt_level.counts_to_v(20000) * CC.MV_PER_V + 0.5)
    assert fr[CC.CC_FLAGS] & CC.FLAG_SCOPE and fr[CC.CC_FLAGS] & CC.FLAG_QUIET
    assert fr[CC.CC_FLAGS] & CC.FLAG_CLEAR and not fr[CC.CC_FLAGS] & CC.FLAG_BUSY
    assert fr[CC.CC_RB] == CC.NONE_14 or fr[CC.CC_RB] < CC.NONE_14
    assert fr[CC.CC_SEQ] == 2


@case
def test_cc_while_ringing_only_the_pt_level_and_flags_at_the_slow_rate():
    h, al, st, port, clock = make(cc=True)
    say(st, al, port, clock, ch=1, scope=True)
    h.tower.tower = "active"
    h._prev_held[0] = True
    n0 = len(h.midi.writes)
    step = A.FRAME_MS // 2
    for _ in range(2 * A.SERIAL_FRAME_BUSY_MS // step):
        clock.t += step
        al.sample()
        al.tick()
    new = h.midi.writes[n0:]
    assert len(new) == 2, len(new)                    # one per SERIAL_FRAME_BUSY_MS
    r = decode(new[-1])[0]
    assert set(r) == {CC.CC_HEAD, CC.CC_FLAGS, CC.CC_PT, CC.CC_PT_SPAN, CC.CC_SEQ}, r
    assert r[CC.CC_FLAGS] & CC.FLAG_BROKE and not r[CC.CC_FLAGS] & CC.FLAG_CLEAR
    assert r[CC.CC_FLAGS] & CC.FLAG_BUSY and not r[CC.CC_FLAGS] & CC.FLAG_QUIET
    assert len(new[-1]) == 7 * 3                      # 28 USB-MIDI bytes


@case
def test_cc_refusals_are_coded_and_thr_mid_sets_mid_range():
    h, al, st, port, clock = make(cc=True)
    say(st, al, port, clock, ch=1, scope=True)
    clock.t += 1000
    say(st, al, port, clock, ch=1, thr_mid=True)
    a = last_ack(h)
    assert a[CC.CC_ACK_WHAT] == CC.WHAT["thr_mid"] and a[CC.CC_ACK_WHY] == 0, a
    assert h.refresher.target[0] == 32768 and a[CC.CC_THR] == CC.duty14(32768)
    h.tower.tower = "active"
    clock.t += 1000
    say(st, al, port, clock, ch=1, thr=1)
    a = last_ack(h)
    assert a[CC.CC_ACK_WHAT] == CC.WHAT["thr"] and a[CC.CC_ACK_WHY] == 2   # tower not quiet
    say(st, al, port, clock, ch=3, scope=True)                              # empty channel
    a = last_ack(h)
    assert a[CC.CC_ACK_WHY] == 1 and a[CC.CC_HEAD] == 3
    assert CC.why_code("something new") == CC.WHY_OTHER and CC.why_code(None) == 0
    assert CC.why_code("laser current over 60 mA") == 8


@case
def test_cc_stops_when_the_host_closes_the_port_and_on_keepalive_timeout():
    h, al, st, port, clock = make(cc=True)
    say(st, al, port, clock, ch=1, scope=True)
    port.connected = False
    n = len(h.midi.writes)
    clock.t += A.SERIAL_FRAME_MS
    al.tick()
    assert al.ch is None and len(h.midi.writes) == n
    port.connected = True
    say(st, al, port, clock, ch=1, scope=True)
    clock.t += A.KEEPALIVE_MS + 1
    al.tick()
    assert al.ch is None
    a = last_ack(h)
    assert a[CC.CC_ACK_WHAT] == CC.WHAT["scope"] and a[CC.CC_ACK_WHY] == 10   # timeout
    n = len(h.midi.writes)
    for _ in range(5):
        clock.t += A.SERIAL_FRAME_MS
        al.tick()
    assert len(h.midi.writes) == n                    # nothing without a keep-alive


@case
def test_cc_values_clamp_and_none_and_a_failed_write_never_raises():
    p = MidiPort()
    c = CC.CcOut(p)
    c.frame(1, 0, None, 70000 * 4, -5, 999.0, None, 0.0004)
    r = decode(p.writes[0])[0]
    assert r[CC.CC_THR] == CC.NONE_14 and r[CC.CC_RB] == CC.MAX_14 and r[CC.CC_DRV] == 0
    for d in (0, 1, 655, 32768, 40000, 65534, 65535):     # the host's inverse is within 4 counts
        assert abs(round(CC.duty14(d) * CC.DUTY_FULL / CC.MAX_14) - d) <= 4, d
    assert CC.duty14(65535) == CC.MAX_14 != CC.NONE_14
    assert r[CC.CC_MA] == CC.MAX_14 and r[CC.CC_PT] == CC.NONE_14 and r[CC.CC_PT_SPAN] == 0

    class Bad:
        def write(self, b):
            raise OSError("usb gone")
    c = CC.CcOut(Bad())
    c.frame(1, 0, 1, 1, 1, 1, 1, 1)
    assert c.errors == 1 and c.n == 0


@case
def test_cc_report_allocates_next_to_nothing():
    import tracemalloc
    c = CC.CcOut(MidiPort())
    c.port.write = lambda b: len(b)
    tracemalloc.start()
    s0 = tracemalloc.take_snapshot()
    for _ in range(200):
        c.frame(1, 9, 32768, 30000, 65535, 4.9, 1.234, 0.002)
    s1 = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grew = sum(d.size_diff for d in s1.compare_to(s0, "filename")
               if d.traceback[0].filename.endswith("heads_cc.py"))
    assert grew < 200, grew


@case
def test_usb_midi_out_keeps_its_raw_port_for_the_cc_reports():
    import midi_ports
    o = midi_ports.MidiOut("usb", None, [65], port="P")
    assert o.port == "P"
    said = []
    got = CC.open_cc([midi_ports.MidiOut("five_pin", None, [65], port="U"), o], log=said.append)
    assert got is not None and got.port == "P" and said
    assert CC.open_cc([], log=said.append) is None


finish()
