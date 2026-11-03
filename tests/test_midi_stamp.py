# test_midi_stamp.py -- midi_stamp: the stroke stamp SysEx after each NoteOn
# (exact bytes, seq, clock messages, settings), and Ring's order of sends.
import json
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import midi_stamp as MS
import ring

results = []


def case(fn):
    try:
        fn()
        results.append(True)
        print("  ok  ", fn.__name__)
    except Exception as e:
        results.append(False)
        print("  FAIL", fn.__name__, repr(e))


class Port:
    def __init__(self, log=None, fail=False):
        self.log = [] if log is None else log
        self.fail = fail

    def write(self, buf):
        if self.fail:
            raise OSError(5, "EIO")
        self.log.append(("sysex", bytes(buf)))


class Pin:
    def __init__(self, v=True):
        self.value = v


class Pwm:
    duty_cycle = 0


@case
def test_exact_bytes_of_a_stroke_stamp():
    port = Port()
    s = MS.MidiStamp(port, lambda: 0, 0)
    t = (0x12 << 28) | (0x34 << 21) | (0x56 << 14) | (0x78 << 7) | 0x09
    s.stroke(3, t)
    assert port.log == [("sysex", bytes([0xF0, 0x7D, 0x53, 0x01, 0x00, 0x01, 0x03,
                                         0x12, 0x34, 0x56, 0x78, 0x09, 0xF7]))], port.log
    assert MS.decode(port.log[0][1]) == (1, 3, t)
    assert MS.STAMP_LEN == 13 and MS.STAMP_T_BITS == 35


@case
def test_every_byte_but_the_frame_is_7_bit_and_a_real_tick_round_trips():
    port = Port()
    s = MS.MidiStamp(port, lambda: 0, 0)
    for t in (0, 1, (1 << 29) - 60000, (1 << 29) + 123, (1 << 35) - 1):
        s.stroke(8, t)
        b = port.log[-1][1]
        assert b[0] == 0xF0 and b[-1] == 0xF7 and all(x < 0x80 for x in b[1:-1]), b
        assert MS.decode(b)[2] == t, (t, MS.decode(b))
    s.stroke(1, (1 << 35) + 5)                     # past 35 bits: wraps (a year of uptime)
    assert MS.decode(port.log[-1][1])[2] == 5


@case
def test_seq_is_14_bit_and_wraps_to_1_never_0():
    port = Port()
    s = MS.MidiStamp(port, lambda: 0, 0)
    s.seq = MS.STAMP_SEQ_MAX - 2
    for _ in range(3):
        s.stroke(1, 1000)
    seqs = [MS.decode(b)[0] for _, b in port.log]
    assert seqs == [16382, 16383, 1], seqs
    assert port.log[1][1][4:6] == bytes([0x7F, 0x7F])


@case
def test_one_buffer_reused_no_allocation_per_stroke():
    port = Port()
    s = MS.MidiStamp(port, lambda: 0, 0)
    buf = s.buf
    s.stroke(1, 5)
    s.stroke(2, 6)
    assert s.buf is buf and isinstance(buf, bytearray) and len(buf) == MS.STAMP_LEN


@case
def test_clock_message_when_quiet_only():
    now = [1000000]
    port = Port()
    s = MS.MidiStamp(port, lambda: now[0], 10000)
    assert s.clock() is True                       # first pass: one at once
    assert MS.decode(port.log[-1][1]) == (0, MS.STAMP_CH_CLOCK, 1000000)
    now[0] += 9999
    assert s.clock() is False and len(port.log) == 1
    now[0] += 1
    assert s.clock() is True and len(port.log) == 2
    now[0] += 5000
    s.stroke(2, now[0] - 3)                        # a stroke restarts the wait
    now[0] += 9000
    assert s.clock() is False
    now[0] += 1000
    assert s.clock() is True
    assert MS.decode(port.log[-1][1]) == (1, 0, now[0])   # seq = the last stroke's
    assert s.status() == {"seq": 1, "sent": 1, "clocks": 3, "errors": 0}


@case
def test_clock_off_with_zero():
    port = Port()
    s = MS.MidiStamp(port, lambda: 5, 0)
    assert s.clock() is False and port.log == []


@case
def test_a_failing_port_is_counted_never_raised():
    s = MS.MidiStamp(Port(fail=True), lambda: 0, 1000)
    s.stroke(1, 10)
    s.clock()
    assert s.errors == 2 and s.sent == 0 and s.clocks == 0
    assert s.clock() is False                      # no retry every pass


@case
def test_settings_default_on_and_overrides():
    import config_loader
    cfg = config_loader.Config(config_loader._deep_merge(config_loader.DEFAULTS, {}))
    assert MS.settings(cfg) == (True, 10000)
    assert MS.settings(None) == (True, 10000)
    assert MS.settings({}) == (True, 10000)        # a deployed settings.json without "midi"
    assert MS.settings({"midi": {"stamp_sysex": False}}) == (False, 10000)
    assert MS.settings({"midi": {"clock_s": 0}}) == (True, 0)
    assert MS.settings({"midi": {"clock_s": "x", "stamp_sysex": "yes"}}) == (True, 10000)
    ex = json.load(open(os.path.join(ROOT, "settings_example.json")))
    assert ex["midi"] == {"stamp_sysex": True, "clock_s": 10}
    assert config_loader.DEFAULTS["midi"] == ex["midi"]


@case
def test_open_stamp_off_or_no_port_is_none():
    assert MS.open_stamp({"midi": {"stamp_sysex": False}}, Port(), lambda: 0) is None
    assert MS.open_stamp({}, None, lambda: 0) is None
    assert isinstance(MS.open_stamp({}, Port(), lambda: 0), MS.MidiStamp)
    assert ring.midi_port() is None                # no usb_midi off the board


def rig(log, clock):
    ins = [Pin(True) for _ in range(8)]
    r = ring.Ring(ins, [Pwm() for _ in range(8)],
                  send_fn=lambda ch, note, on: log.append(("note", ch, note, on)),
                  clock_ms=clock, stamp=MS.MidiStamp(Port(log), clock, 0))
    return r, ins


@case
def test_ring_head_stroke_note_on_then_its_stamp_then_nothing_on_off():
    log, t = [], [700000]
    r, ins = rig(log, lambda: t[0])
    r.start({0: "head_new", 7: "head_new"})
    r.edge(7, True, 650000)                        # stamped 50 s before "now"
    r.edge(7, False, 650100)
    assert [e[0] for e in log] == ["note", "sysex", "note"], log
    assert log[0] == ("note", 7, 53, True) and log[2] == ("note", 7, 53, False)
    assert MS.decode(log[1][1]) == (1, 8, 650000)  # bell ch 8, the edge's stamp
    r.edge(0, True)                                # no stamp: now
    assert MS.decode(log[-1][1]) == (2, 1, 700000)
    assert r.status()["stamp"]["sent"] == 2


@case
def test_ring_double_gives_no_note_and_no_stamp():
    log, t = [], [0]
    r, ins = rig(log, lambda: t[0])
    r.start({0: "head_new"})
    r.edge(0, True, 1000)
    r.edge(0, False, 1020)
    r.edge(0, True, 1054)                          # inside MIN_STROKE_GAP_MS
    assert [e[0] for e in log] == ["note", "sysex", "note"], log


@case
def test_ring_trigger_note_carries_the_press_stamp_and_off_has_none():
    log, t = [], [900000]
    r, ins = rig(log, lambda: t[0])
    r.start({2: "trigger"})
    r.note(2, True, 899990)
    r.note(2, False)
    r.stop()
    assert [e[0] for e in log] == ["note", "sysex", "note"], log
    assert MS.decode(log[1][1]) == (1, 3, 899990)


@case
def test_ring_without_stamp_is_as_before():
    sent = []
    r = ring.Ring([Pin(True)], [Pwm()], send_fn=lambda ch, note, on: sent.append(on))
    r.start({0: "head_new"})
    r.edge(0, True)
    r.clock()
    assert sent == [True] and "stamp" not in r.status()


@case
def test_ring_clock_passes_through():
    log, t = [], [123456]
    ins = [Pin(True)]
    r = ring.Ring(ins, [Pwm()], send_fn=lambda *a: None, clock_ms=lambda: t[0],
                  stamp=MS.MidiStamp(Port(log), lambda: t[0], 10000))
    r.clock()
    r.clock()
    assert [MS.decode(b) for _, b in log] == [(0, 0, 123456)]


@case
def test_files_txt_deploys_it():
    names = open(os.path.join(ROOT, "tools", "files.txt")).read().split()
    assert "midi_stamp.py" in names


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
sys.exit(1 if n_fail else 0)
