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


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
raise SystemExit(1 if n_fail else 0)
