# test_midi_ports.py -- usb / five_pin settings and fan-out from Ring
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import midi_ports as MP
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
    def __init__(self):
        self.writes = []

    def write(self, buf):
        self.writes.append(bytes(buf))


class Sink:
    def __init__(self, name):
        self.name = name
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


@case
def test_flat_midi_is_usb_only():
    usb, five = MP.parse_ports({"midi": {"stamp_sysex": False, "clock_s": 0}})
    assert usb["enabled"] is True
    assert usb["channel"] == 1
    assert usb["notes"] == list(MP.DEFAULT_NOTES)
    assert usb["stamp_sysex"] is False
    assert usb["clock_s"] == 0
    assert five["enabled"] is False
    assert five["tx_pin"] == 16
    assert five["stamp_sysex"] is True


@case
def test_nested_ports_keep_separate_notes_channel_sysex():
    cfg = {"midi": {
        "usb": {
            "enabled": True,
            "channel": 1,
            "notes": [65, 64, 62, 60, 58, 57, 55, 53],
            "stamp_sysex": True,
            "clock_s": 10,
        },
        "five_pin": {
            "enabled": True,
            "tx_pin": 16,
            "channel": 3,
            "notes": [72, 71, 69, 67, 65, 64, 62, 60],
            "stamp_sysex": False,
            "clock_s": 0,
        },
    }}
    usb, five = MP.parse_ports(cfg)
    assert usb["channel"] == 1 and usb["stamp_sysex"] is True and usb["notes"][0] == 65
    assert five["enabled"] is True
    assert five["channel"] == 3 and five["tx_pin"] == 16
    assert five["stamp_sysex"] is False and five["clock_s"] == 0
    assert five["notes"][0] == 72


@case
def test_bad_channel_falls_back_to_one():
    usb, _ = MP.parse_ports({"midi": {"usb": {"channel": 99}}})
    assert usb["channel"] == 1


@case
def test_midiout_and_ring_fan_out_per_port_notes():
    orig = MP._note_msg
    MP._note_msg = lambda note, on: (note, on)
    try:
        usb_sink, din_sink = Sink("usb"), Sink("five_pin")
        usb = MP.MidiOut("usb", usb_sink, [65, 64], stamp=None)
        din = MP.MidiOut("five_pin", din_sink, [72, 71], stamp=None)
        ins = [type("P", (), {"value": True})()]
        las = [type("W", (), {"duty_cycle": 0})()]
        r = ring.Ring(ins, las, outputs=[usb, din])
        r.note(0, True, 1000)
        r.note(0, False, 1100)
        assert usb_sink.sent == [(65, True), (65, False)]
        assert din_sink.sent == [(72, True), (72, False)]
    finally:
        MP._note_msg = orig


@case
def test_open_outputs_skips_five_pin_when_tx_is_neopixel():
    cfg = {"midi": {"usb": {"enabled": False},
                    "five_pin": {"enabled": True, "tx_pin": 17}}}
    outs = MP.open_outputs(cfg, lambda: 0, neopixel_gp=17)
    assert outs == []


@case
def test_defaults_match_example_shape():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = json.load(open(os.path.join(root, "settings_example.json")))
    usb, five = MP.parse_ports(ex)
    assert usb["enabled"] is True and usb["channel"] == 1
    assert five["enabled"] is False and five["tx_pin"] == 16
    assert usb["notes"] == five["notes"] == list(MP.DEFAULT_NOTES)


@case
def test_files_txt_deploys_midi_ports():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = open(os.path.join(root, "tools", "files.txt")).read().split()
    assert "midi_ports.py" in names


class Boom:
    """A USB MIDI out whose host has dropped the link (dwc_otg reset)."""
    def __init__(self):
        self.tries = 0

    def send(self, msg):
        self.tries += 1
        raise OSError(5, "EIO")


class Stamp:
    def __init__(self, fail=False):
        self.strokes, self.clocks, self.fail = [], 0, fail

    def stroke(self, ch, t):
        if self.fail:
            raise ValueError("stamp")
        self.strokes.append((ch, t))

    def clock(self):
        self.clocks += 1

    def status(self):
        return {"sent": len(self.strokes)}


def _ring(outs):
    ins = [type("P", (), {"value": True})()]
    las = [type("W", (), {"duty_cycle": 0})()]
    return ring.Ring(ins, las, outputs=outs)


def _patched(fn):
    def run():
        orig = MP._note_msg
        MP._note_msg = lambda note, on: (note, on)
        try:
            fn()
        finally:
            MP._note_msg = orig
    run.__name__ = fn.__name__
    return run


@case
def test_open_outputs_puts_five_pin_first_and_usb_gets_a_ready_test():
    o_usb, o_din = MP.open_usb_midi, MP.open_five_pin_midi
    MP.open_usb_midi = lambda ch: (Sink("usb"), Port())
    MP.open_five_pin_midi = lambda pin, ch: (Sink("din"), Port())
    try:
        cfg = {"midi": {"usb": {"enabled": True, "stamp_sysex": False},
                        "five_pin": {"enabled": True, "tx_pin": 16, "stamp_sysex": False}}}
        outs = MP.open_outputs(cfg, lambda: 0, ready=lambda: True)
        assert [o.name for o in outs] == ["five_pin", "usb"]
        assert outs[0].ready is None and outs[1].ready() is True
        assert MP.usb_ready() is None              # host: no supervisor module
    finally:
        MP.open_usb_midi, MP.open_five_pin_midi = o_usb, o_din


@case
@_patched
def test_a_failing_usb_never_stops_five_pin_and_is_logged_rate_limited():
    logs = []
    din_sink, bad = Sink("din"), Boom()
    din = MP.MidiOut("five_pin", din_sink, [72], stamp=Stamp())
    usb_stamp = Stamp()
    usb = MP.MidiOut("usb", bad, [65], stamp=usb_stamp, log=logs.append)
    for outs in ([din, usb], [usb, din]):             # either order: the other port still sends
        r = _ring(outs)
        r.note(0, True, 1000)
    assert din_sink.sent == [(72, True), (72, True)]
    assert usb.errors == 2 and usb_stamp.strokes == []   # no stamp after a failed NoteOn
    for _ in range(2 * MP.OUT_ERR_LOG_EVERY):
        usb.send(0, False, 0)
    assert usb.errors == 2 + 2 * MP.OUT_ERR_LOG_EVERY
    assert len(logs) == 3                              # 1st, 101st, 201st
    assert "usb" in logs[0] and "EIO" in logs[0]
    assert r.status()["midi"]["usb"]["errors_out"] == usb.errors


@case
@_patched
def test_usb_is_skipped_while_the_host_is_gone():
    up = [False]
    sink, st = Sink("usb"), Stamp()
    usb = MP.MidiOut("usb", sink, [65], stamp=st, ready=lambda: up[0])
    assert usb.send(0, True, 5) is False
    usb.clock()
    assert sink.sent == [] and st.strokes == [] and st.clocks == 0 and usb.skipped == 1
    up[0] = True
    assert usb.send(0, True, 6) is True
    usb.clock()
    assert sink.sent == [(65, True)] and st.strokes == [(1, 6)] and st.clocks == 1


@case
@_patched
def test_a_failing_stamp_is_counted_and_the_note_still_went():
    sink = Sink("usb")
    usb = MP.MidiOut("usb", sink, [65], stamp=Stamp(fail=True), log=lambda m: None)
    assert usb.send(0, True, 1) is True
    assert sink.sent == [(65, True)] and usb.errors == 1


@case
@_patched
def test_send_allocates_nothing_of_its_own():
    import tracemalloc
    msg = (65, True)
    MP._note_msg = lambda note, on: msg
    usb = MP.MidiOut("usb", type("S", (), {"send": lambda self, m: None})(), [65],
                     ready=lambda: True)
    gone = MP.MidiOut("usb", Boom(), [65], ready=lambda: False)
    usb.send(0, True, 0)
    gone.send(0, True, 0)
    tracemalloc.start()
    a = tracemalloc.get_traced_memory()[0]
    for _ in range(2000):
        usb.send(0, True, 0)
        gone.send(0, True, 0)
    b = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert b - a < 512, b - a                          # nothing per send (CPython int caches aside)


@case
def test_ring_legacy_send_errors_never_escape():
    ins = [type("P", (), {"value": True})()]
    las = [type("W", (), {"duty_cycle": 0})()]
    def boom(ch, note, on):
        raise OSError(19, "ENODEV")
    r = ring.Ring(ins, las, send_fn=boom)
    for _ in range(3):
        r.note(0, True, 1)
    assert r.send_errors == 3


@case
def test_cc_reports_skip_a_gone_host_and_count_short_and_failed_writes():
    import heads_cc as H
    up = [False]

    class Short:
        def __init__(self, take):
            self.take = take

        def write(self, b):
            if self.take is None:
                raise OSError(5, "EIO")
            return min(self.take, len(b))

    c = H.CcOut(Short(9), ready=lambda: up[0])
    c.frame(1, 0, 1, 2, 3, 1.0, 1.0, 0.0)
    assert (c.skipped, c.reports, c.short) == (1, 0, 0)
    up[0] = True
    c.frame(1, 0, 1, 2, 3, 1.0, 1.0, 0.0)
    assert (c.reports, c.short) == (0, 1)
    c.port = Short(None)
    c.frame(1, 0, 1, 2, 3, 1.0, 1.0, 0.0)
    assert c.errors == 1
    c.port = Short(1000)
    c.frame(1, 0, 1, 2, 3, 1.0, 1.0, 0.0)
    assert c.reports == 1
    out = MP.MidiOut("usb", None, [65], port=Port(), ready=lambda: up[0])
    assert H.open_cc([out], log=lambda m: None).ready is out.ready


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
raise SystemExit(1 if n_fail else 0)
