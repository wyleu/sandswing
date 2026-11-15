# test_tune.py -- live threshold / laser-duty commands (RAM only)
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import *  # noqa: E402  FakePinBank, PINS, case, finish, ...
import channel_gate
import config_loader
import mux_select
import threshold_refresh
import tower_build
import tune
from signal_bands import COUNTS_PER_MA, FLOOR_COUNTS


def settings_ok():
    mux_select.reset_registry()


def make_host(kinds=None, duty=65535, dark=None):
    """A duck-typed concentrator: 4 bells, ch0/ch1 kinds default head_new."""
    settings_ok()
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    for i, b in enumerate(bells):
        b.laser = bank.pwm_out(8 + i, frequency=1000, duty_cycle=duty if i < 2 else 0)
    gate = channel_gate.ChannelGate(4)
    kinds = kinds or {0: "head_new", 1: "head_old", 2: "empty", 3: "empty"}
    for i, k in kinds.items():
        gate.set_kind(i, k)
    r = threshold_refresh.ThresholdRefresher(
        tower, bells[0].drive, bells, gate=gate, clock=bank.clock)
    if kinds.get(0) == "head_new":
        r.set_target(bells[0], r.default_duty)
        r.apply_once()
    last_result = {}
    if dark is not None:
        last_result[0] = {"m": {"dark": dark}}

    class Tower:
        tower = "inactive"

    class Survey:
        phase = "idle"

    survey = Survey()
    survey.last_result = last_result

    class Host:
        n = 4
        def __init__(self):
            self.bells = bells
            self.mux = tower
            self.tower = Tower()
            self.gate = gate
            self.refresher = r
            self.survey_state = survey
            self.bank = bank
    return Host()


@case
def test_parse_commands():
    assert tune.parse_command('{"cmd":"tune","ch":1,"thr":32768}', 8) == (
        "tune", 0, "thr", 32768)
    assert tune.parse_command('{"cmd":"tune","ch":2,"thr_step":-3}', 8) == (
        "tune", 1, "thr_step", -1)
    assert tune.parse_command('{"cmd":"drive","ch":8,"drive":16384}', 8) == (
        "drive", 7, "drive", 16384)
    assert tune.parse_command('{"cmd":"drive","ch":1,"drive_step":1}', 8) == (
        "drive", 0, "drive_step", 1)
    assert tune.parse_command('{"cmd":"tune_get"}', 8) == (
        "tune_get", None, None, None)
    assert tune.parse_command('{"cmd":"tune_get","ch":3}', 8) == (
        "tune_get", 2, None, None)
    for bad in ('{', '[]', '{"cmd":"wifi_off"}', '{"cmd":"tune","ch":0,"thr":1}',
                '{"cmd":"tune","ch":1}', '{"cmd":"tune","ch":1,"thr":1,"thr_step":1}',
                '{"cmd":"tune","ch":1,"thr":true}', '{"cmd":"drive","ch":1,"extra":1}',
                '{"cmd":"tune","ch":1,"thr_step":0}', '{"cmd":"tune_get","ch":9}'):
        try:
            tune.parse_command(bad, 8)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted %r" % bad)


@case
def test_laser_and_tune_settings_defaults():
    s = tune.laser_settings(None)
    assert s == {"min_duty": tune.LASER_MIN_DUTY, "max_duty": tune.LASER_MAX_DUTY,
                 "step": tune.LASER_STEP, "max_ma": tune.LASER_MAX_MA}
    s = tune.laser_settings({"laser": {"min_duty": 20000, "max_duty": 10000,
                                       "step": 0, "max_ma": -1}})
    assert s["min_duty"] == 10000 and s["max_duty"] == 20000
    assert s["step"] == tune.LASER_STEP and s["max_ma"] == tune.LASER_MAX_MA
    ts = tune.tune_settings(None)
    assert ts["threshold"]["step"] == threshold_refresh.STEP
    assert ts["min_gap_ms"] == tune.TUNE_MIN_GAP_MS
    import json
    ex = json.load(open(os.path.join(ROOT, "settings_example.json")))
    assert ex["laser"] == config_loader.DEFAULTS["laser"]
    assert ex["laser"] == {"min_duty": 16384, "max_duty": 65535,
                           "step": 1311, "max_ma": 60.0}
    assert ex["threshold"]["step"] == 655
    names = open(os.path.join(ROOT, "tools", "files.txt")).read().split()
    assert "tune.py" in names


@case
def test_set_threshold_absolute_and_step():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 1000)
    ack = t.handle("tune", 0, "thr", 40000, 1000)
    assert ack["ok"] is True and ack["thr"] == 40000 and ack["t"] == "tune_ack"
    assert host.refresher.target[0] == 40000
    ack = t.handle("tune", 0, "thr_step", 1, 2000)
    assert ack["ok"] and ack["thr"] == 40000 + threshold_refresh.STEP
    # at max: step up refused
    t.handle("tune", 0, "thr", 65535, 3000)
    ack = t.handle("tune", 0, "thr_step", 1, 4000)
    assert ack["ok"] is False and ack["why"] == "at limit"
    # clamp absolute
    ack = t.handle("tune", 0, "thr", 999999, 5000)
    assert ack["ok"] and ack["thr"] == 65535


@case
def test_refuse_old_head_empty_ringing_survey_too_fast():
    host = make_host()
    now = [1000]
    t = tune.Tuner(host, clock_ms=lambda: now[0])
    ack = t.handle("tune", 1, "thr", 40000, 1000)     # head_old
    assert ack["ok"] is False and "no threshold" in ack["why"]
    ack = t.handle("drive", 2, "drive", 20000, 1000)  # empty, laser off anyway
    assert ack["ok"] is False and "not an active head" in ack["why"]
    host.tower.tower = "active"
    ack = t.handle("tune", 0, "thr", 40000, 1000)
    assert ack["ok"] is False and "not quiet" in ack["why"]
    host.tower.tower = "inactive"
    host.survey_state.phase = "surveying"
    ack = t.handle("tune", 0, "thr", 40000, 1000)
    assert ack["ok"] is False and "survey" in ack["why"]
    host.survey_state.phase = "idle"
    ack = t.handle("tune", 0, "thr", 40000, 1000)
    assert ack["ok"] is True
    ack = t.handle("tune", 0, "thr", 41000, 1100)     # 100 ms later
    assert ack["ok"] is False and ack["why"] == "too fast"
    ack = t.handle("tune", 0, "thr", 41000, 1000 + tune.TUNE_MIN_GAP_MS)
    assert ack["ok"] is True and ack["thr"] == 41000


@case
def test_drive_override_kept_in_map_and_clamped():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    # clock advances by hand
    ack = t.handle("drive", 0, "drive", 20000, 1000)
    assert ack["ok"] is True and ack["drv"] == 20000
    assert host.bells[0].laser.duty_cycle == 20000
    assert t.drive_map() == {0: 20000}
    ack = t.handle("drive", 0, "drive_step", -1, 2000)
    assert ack["ok"] and ack["drv"] == 20000 - tune.LASER_STEP
    # floor
    t.handle("drive", 0, "drive", 0, 3000)            # clamps to min, not off
    assert host.bells[0].laser.duty_cycle == tune.LASER_MIN_DUTY
    # laser off refused
    host.bells[0].laser.duty_cycle = 0
    ack = t.handle("drive", 0, "drive", 20000, 4000)
    assert ack["ok"] is False and ack["why"] == "laser is off"


@case
def test_refuse_raise_when_current_at_cap():
    host = make_host(dark=0)
    host.bells[0].sense.adc.value = int(tune.LASER_MAX_MA * COUNTS_PER_MA)
    t = tune.Tuner(host, clock_ms=lambda: 0)
    host.bells[0].laser.duty_cycle = 20000
    ack = t.handle("drive", 0, "drive", 30000, 1000)  # a raise
    assert ack["ok"] is False and "laser current over" in ack["why"], ack
    assert host.bells[0].laser.duty_cycle == 20000    # unchanged
    # a lower is still allowed
    ack = t.handle("drive", 0, "drive", 18000, 2000)
    assert ack["ok"] is True and ack["drv"] == 18000


@case
def test_refuse_raise_when_mux_busy():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    host.bells[0].laser.duty_cycle = 20000
    with host.mux.select(host.bells[1]):
        ack = t.handle("drive", 0, "drive", 30000, 1000)
    assert ack["ok"] is False and ack["why"] == "busy"
    assert host.bells[0].laser.duty_cycle == 20000


@case
def test_tune_get_one_and_all():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    ack = t.handle("tune_get", 0, None, None, 1000)
    assert ack["t"] == "tune" and ack["ok"] and ack["ch"] == 1
    assert ack["thr"] == host.refresher.default_duty
    assert "drv" in ack and "ma" in ack
    ack = t.handle("tune_get", None, None, None, 1000)
    chs = [h["ch"] for h in ack["heads"]]
    assert chs == [1, 2]                               # new + old
    assert ack["heads"][0]["thr"] == host.refresher.default_duty
    assert ack["heads"][1]["thr"] is None              # old: no target


@case
def test_nothing_writes_settings():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    t.handle("tune", 0, "thr", 40000, 1000)
    t.handle("drive", 0, "drive", 20000, 2000)
    assert not hasattr(t, "save")
    src = open(os.path.join(ROOT, "src", "tune.py")).read()
    assert "settings.json is not written" in src or "Nothing here writes settings.json" in src
    assert "cfg.save" not in src


class Port:
    """usb_cdc.data stand-in: bounded writes, injectable inbound."""

    def __init__(self, room=10000, connected=True):
        self.room, self.connected = room, connected
        self.out = bytearray()
        self._in = bytearray()
        self.timeout = None
        self.write_timeout = None

    def feed(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._in += data

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


@case
def test_serial_tune_command_and_ack():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    p = Port()
    s = tune.SerialTune(p, t)
    p.feed('{"cmd":"tune","ch":1,"thr":40000}\n')
    wait = s.poll(1.0, busy=False)
    assert wait == tune.TUNE_POLL_S
    assert s.lines == 1 and s.acks == 1 and s.bad == 0
    ack = json.loads(bytes(p.out))
    assert ack["t"] == "tune_ack" and ack["ok"] is True and ack["thr"] == 40000
    assert host.refresher.target[0] == 40000


@case
def test_serial_tune_ack_waits_while_busy_then_flushes():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    p = Port()
    s = tune.SerialTune(p, t)
    p.feed('{"cmd":"tune_get","ch":1}\n')
    s.poll(1.0, busy=True)
    assert s.lines == 1 and s.acks == 0 and p.out == b""   # held
    s.poll(1.1, busy=False)
    assert s.acks == 1
    ack = json.loads(bytes(p.out))
    assert ack["t"] == "tune" and ack["ch"] == 1


@case
def test_serial_tune_partial_ack_and_overflow_and_bad_never_raise():
    host = make_host()
    t = tune.Tuner(host, clock_ms=lambda: 0)
    p = Port(room=8)
    s = tune.SerialTune(p, t)
    p.feed('{"cmd":"tune_get"}\n')
    s.poll(1.0)
    assert s._ack is not None and s.acks == 0          # part-written
    p.room = 10000
    s.poll(1.1)
    assert s.acks == 1 and s._ack is None
    # overflow: a flood without newlines
    p = Port()
    s = tune.SerialTune(p, t, buf_max=32)
    p.feed(b"x" * 40)
    s.poll(2.0)
    assert s.overflows >= 1
    # bad json / unknown command counted, ack why, no raise
    p.feed(b"not json\n")
    s.poll(3.0)
    assert s.bad >= 1
    p.out = bytearray()
    p.feed(b'{"cmd":"nope"}\n')
    s.poll(4.0)
    ack = json.loads(bytes(p.out).split(b"\n")[-2] or bytes(p.out).split(b"\n")[0])
    assert ack["ok"] is False and ack["t"] == "tune_ack"
    # disconnected: nothing blows up
    p.connected = False
    p.feed(b'{"cmd":"tune_get"}\n')
    s.poll(5.0)


@case
def test_open_serial_tune_none_without_a_port():
    said = []
    assert tune.open_serial_tune(None, object(), log=said.append) is None
    host = make_host()
    t = tune.Tuner(host)
    p = Port()
    s = tune.open_serial_tune(p, t, log=said.append)
    assert s is not None and s.port is p
    assert any("settings.json" in x and "not written" in x for x in said)


@case
def test_tune_get_carries_the_pt_level_only_on_the_wired_channel():
    from pt_level import PtLevel

    class ADC:
        value = 31000
    h = make_host()
    h.pt_level = PtLevel(ADC(), 0, 27)
    t = tune.Tuner(h, None, clock_ms=lambda: 0)
    a = t.handle("tune_get", 0, None, None)
    assert a["pt"] == round(31000 * 3.3 / 65535, 3) and a["pt_span"] == 0.0, a
    b = t.handle("tune_get", 1, None, None)
    assert b["pt"] is None and b["pt_span"] is None
    h2 = make_host()                              # no pt_level at all (off in settings)
    c = tune.Tuner(h2, None, clock_ms=lambda: 0).handle("tune_get", 0, None, None)
    assert c["pt"] is None and c["ok"]



class CPBuffers:
    """with CPBuffers(): every bytearray tune.py makes is a CPBytearray."""

    def __enter__(self):
        tune.bytearray = CPBytearray       # module global shadows the builtin
        return self

    def __exit__(self, *exc):
        del tune.bytearray
        return False


def cp_serial_tune(port):
    s = tune.SerialTune(port, tune.Tuner(make_host(), clock_ms=lambda: 0))
    assert isinstance(s._buf, CPBytearray)
    return s


@case
def test_serial_tune_reads_lines_with_cp_bytearray():
    with CPBuffers():
        p = Port()
        s = cp_serial_tune(p)
        # two lines in one read, then a line split across reads, CRLF on one
        p.feed('{"cmd":"tune_get","ch":1}\n{"cmd":"tune","ch":1,"thr":40000}\r\n{"cmd":"tu')
        s.poll(1.0, busy=False)
        assert isinstance(s._buf, CPBytearray) and bytes(s._buf) == b'{"cmd":"tu'
        p.feed('ne_get","ch":1}\n')
        s.poll(1.1, busy=False)
        s.poll(1.2, busy=False)
    assert s.errors == 0, s.last_error
    assert s.lines == 3 and s.bad == 0, s.counters()
    acks = [json.loads(x) for x in bytes(p.out).split(b"\n") if x.strip()]
    assert [a["t"] for a in acks] == ["tune", "tune_ack", "tune"], acks
    assert acks[1]["ok"] is True and acks[1]["thr"] == 40000


@case
def test_serial_tune_old_slice_delete_is_what_cp_refuses():
    # the pre-fix line, on a CircuitPython-like buffer: TypeError
    b = CPBytearray(b"ab\ncd")
    expect(TypeError, b.__delitem__, slice(None, 3))
    assert bytes(b[3:]) == b"cd" and isinstance(b[3:], CPBytearray)


@case
def test_brief_is_the_small_counter_set():
    with CPBuffers():
        p = Port()
        s = cp_serial_tune(p)
        b = s.brief()
        assert tuple(b) == tune.BRIEF_KEYS or sorted(b) == sorted(tune.BRIEF_KEYS)
        assert set(b.values()) == {0, None}
        seen = []
        s.align_rx = seen.append
        p.feed(b'not json\n{"cmd":"tune_get","ch":1}\n{"cmd":"align","ch":1,"scope":true}\n')
        s.poll(1.0, busy=False)
    b = s.brief()
    assert (b["lines"], b["align"], b["bad"], b["errors"]) == (1, 1, 1, 0), b
    assert len(seen) == 1 and b["last_error"] is None


# -- laser mA on a chopped current (the 1 kHz laser PWM) ---------------------
import adc_avg  # noqa: E402
import heads_cc  # noqa: E402

FULL_ON_MA = 29.6               # head 5's survey (full duty, DC)
DUTIES = (0.25, 0.52, 1.0)
AVG_TOL_MA = 0.35               # each reading's average vs duty x FULL_ON_MA
ON_TOL_MA = 1.2                 # each reading's on-current vs FULL_ON_MA
READINGS = 24


def chop_host(duty_frac, dark=FLOOR_COUNTS, seed=5, **kw):
    host = make_host(dark=dark)
    rig = ChopRig(duty_frac, int(round(FULL_ON_MA * COUNTS_PER_MA)), dark,
                  rng=random.Random(seed), **kw)
    host.bells[0].sense.adc = rig
    host.bells[0].laser.duty_cycle = int(round(duty_frac * tune.FULL))
    return host, rig, tune.Tuner(host, clock_ms=lambda: 0, now_ns=rig.now_ns)


def readings(t, rig, n=READINGS):
    out = []
    for _ in range(n):
        rig.new_burst()
        assert t._measure(0) is not False
        out.append((t.ma[0], t.ma_on[0]))
    return out


@case
def test_laser_ma_is_steady_on_a_chopped_current():
    for duty in DUTIES:
        host, rig, t = chop_host(duty)
        got = readings(t, rig)
        want = duty * FULL_ON_MA
        for ma, on in got:
            assert abs(ma - want) <= AVG_TOL_MA, (duty, ma, want)
            assert abs(on - FULL_ON_MA) <= ON_TOL_MA, (duty, on)
        assert t.burst_us is not None and t.burst_us <= tune.TUNE_BURST_MAX_MS * 1000


@case
def test_old_32_read_burst_aliases_where_the_paced_one_does_not():
    host, rig, t = chop_host(0.25)
    old = []
    for _ in range(READINGS):
        rig.new_burst()
        old.append((adc_avg.avg(rig, 32) - FLOOR_COUNTS) / COUNTS_PER_MA)
    new = [ma for ma, _ in readings(t, rig)]
    assert max(old) - min(old) > 10 * (max(new) - min(new) + AVG_TOL_MA / 10), (old, new)


@case
def test_slow_or_fast_loop_still_steady():
    # a slower Pico loop (fewer reads fit) and a faster one
    for call_ns, read_ns in ((20000, 25000), (3000, 5000)):
        for duty in DUTIES:
            host, rig, t = chop_host(duty, call_ns=call_ns, read_ns=read_ns)
            for ma, on in readings(t, rig):
                assert abs(on - FULL_ON_MA) <= 2 * ON_TOL_MA, (call_ns, duty, on)


@case
def test_burst_is_whole_pwm_periods_and_short():
    host, rig, t = chop_host(0.5)
    class P:
        pass
    p = P()
    for hz, periods in ((1000, tune.TUNE_ADC_PERIODS), (20000, tune.TUNE_ADC_PERIODS),
                        (500, 2), (100, 1)):
        p.frequency = hz
        assert t.burst_ns(p) == periods * (tune.NS_PER_S // hz), (hz, t.burst_ns(p))
    p.frequency = 1000
    assert t.burst_ns(p) <= tune.TUNE_BURST_MAX_MS * tune.NS_PER_MS
    del p.frequency                                   # no frequency: LASER_HZ
    assert t.burst_ns(p) == tune.TUNE_ADC_PERIODS * (tune.NS_PER_S // tune.LASER_HZ)
    a, b = tune.TUNE_ADC_N, tune.TUNE_ADC_PERIODS
    while b:
        a, b = b, a % b
    assert a == 1                                     # coprime: phases do not lock


@case
def test_first_read_after_select_is_discarded():
    host, rig, t = chop_host(1.0, stale=60000)
    rig.new_burst()
    t._measure(0)
    assert abs(t.ma[0] - FULL_ON_MA) <= AVG_TOL_MA, t.ma[0]


@case
def test_paced_avg_never_reads_past_the_window():
    rig = ChopRig(0.5, 1000, 0, call_ns=40000, read_ns=40000, jitter=0.0)
    window = tune.TUNE_ADC_PERIODS * tune.NS_PER_S // tune.LASER_HZ
    mean, k, took = adc_avg.paced_avg(rig, tune.TUNE_ADC_N, window, rig.now_ns)
    assert 0 < k < tune.TUNE_ADC_N
    assert took <= window + 3 * 40000 + ChopRig.PICO_TICK_NS


@case
def test_on_current_none_at_duty_0_and_bursts_restart_on_a_drive_change():
    host, rig, t = chop_host(0.25)
    readings(t, rig, tune.TUNE_MA_BURSTS + 2)
    assert len(t._bursts[0][1]) == tune.TUNE_MA_BURSTS
    host.bells[0].laser.duty_cycle = 0
    rig.duty = 0.0
    readings(t, rig, 1)
    assert t.ma[0] == 0 and t.ma_on[0] is None and len(t._bursts[0][1]) == 1


@case
def test_raise_refused_on_the_on_current_not_the_average():
    host, rig, t = chop_host(0.25)
    t.settings["laser"]["max_ma"] = FULL_ON_MA - 5     # average 7.4 is under, on 29.6 over
    ack = t.handle("drive", 0, "drive_step", 1, 1000)
    assert ack["ok"] is False and "laser current over" in ack["why"], ack
    assert ack["ma"] < t.settings["laser"]["max_ma"] < ack["ma_on"]


@case
def test_cc_carries_the_average_and_the_host_gets_on_current_from_drive():
    host, rig, t = chop_host(0.52)
    readings(t, rig, 4)
    ma, on = t.ma[0], t.ma_on[0]
    drv14 = heads_cc.duty14(host.bells[0].laser.duty_cycle)
    ma14 = heads_cc.ma14(ma)
    # the host side: ma / (drv / full), from the two CCs only
    host_on = (ma14 / heads_cc.MA_UNITS) / (drv14 / heads_cc.MAX_14)
    assert abs(host_on - on) <= 0.05, (host_on, on)

finish()
