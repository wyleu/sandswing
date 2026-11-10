# test_serial_status.py -- the heartbeat as JSON lines on usb_cdc.data
import json
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import serial_status as SS
import link_health
import config_loader

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
    """usb_cdc.Serial stand-in: takes at most room bytes per write."""
    def __init__(self, room=10000, connected=True):
        self.room, self.connected = room, connected
        self.out = bytearray()
        self.write_timeout = None

    def write(self, buf):
        n = min(len(buf), self.room)
        self.out += bytes(buf[:n])
        return n


def doc():
    return {"schema": "sand.panel/v1", "type": "heartbeat", "src": "sandswing",
            "id": "x", "boot_id": "ab12", "seq": 3, "utc_ms": 1,
            "body": {"panel": [[0, 0, 0]], "panel_state": "ok", "sync": "synced",
                     "uid": "7091CDAB9DBD5305", "chan": [["head_new", None]],
                     "link": "no_wifi", "tower": "inactive", "uptime_s": 99,
                     "net": {"drops": 0, "big": list(range(50))},
                     "wifi": {"ip": None, "rssi": None, "ssid_secret": "no"},
                     "time": None, "mem_free": 12345, "gc_heap_kb": 80,
                     "edges_lost": 0, "late_drains": 1, "strokes_undelivered": 0,
                     "watchdog": False, "unlisted": "dropped"}}


def lines(port):
    return [l for l in bytes(port.out).split(b"\n")]


@case
def test_one_whole_line_when_due_and_slim():
    p = Port()
    s = SS.SerialStatus(p, doc, period_s=10, now=0.0)
    assert p.write_timeout == 0
    wait = s.step(0.0)
    assert wait == 10.0
    assert p.out.endswith(b"\n") and p.out.count(b"\n") == 1
    d = json.loads(bytes(p.out))
    assert d["schema"] == "sand.panel/v1" and d["type"] == "heartbeat"
    assert d["via"] == SS.SERIAL_VIA
    b = d["body"]
    assert b["uid"] == "7091CDAB9DBD5305" and b["mem_free"] == 12345
    assert b["late_drains"] == 1 and b["watchdog"] is False
    assert "unlisted" not in b and "ssid_secret" not in b["wifi"]
    assert "big" not in b["net"]
    assert d["serial"]["period_s"] == 10 and d["serial"]["lines"] == 0
    assert s.lines == 1
    assert s.step(5.0) == 5.0 and p.out.count(b"\n") == 1     # not due yet


@case
def test_same_fields_as_udp_heartbeat():
    p = Port()
    s = SS.SerialStatus(p, doc, now=0.0)
    s.step(0.0)
    d = json.loads(bytes(p.out))
    slim = json.loads(json.dumps(link_health.udp_heartbeat_doc(doc())))
    for k, v in slim["body"].items():
        assert d["body"][k] == v, k
    for k in link_health.HB_UDP_ENVELOPE:
        assert d[k] == slim[k], k


@case
def test_nothing_built_while_no_host():
    built = []
    p = Port(connected=False)
    s = SS.SerialStatus(p, lambda: built.append(1) or doc(), now=0.0)
    s.step(0.0)
    s.step(10.0)
    assert built == [] and p.out == b"" and s.unread == 2
    p.connected = True
    s.step(20.0)
    # first line after the host came: starts on a fresh line
    assert p.out.startswith(b"\n") and json.loads(bytes(p.out).strip())["seq"] == 3


@case
def test_partial_writes_finish_later_never_wait():
    p = Port(room=100)
    s = SS.SerialStatus(p, doc, now=0.0)
    w = s.step(0.0)
    assert w == SS.SERIAL_PUSH_S and len(p.out) == 100 and s.lines == 0
    t = 0.0
    for _ in range(100):
        t += w
        w = s.step(t)
        if s.lines:
            break
    assert s.lines == 1 and p.out.count(b"\n") == 1
    json.loads(bytes(p.out))


@case
def test_stuck_line_given_up_next_starts_fresh():
    p = Port(room=50)
    s = SS.SerialStatus(p, doc, period_s=10, now=0.0)
    s.step(0.0)
    p.room = 0                       # host stopped reading
    for t in (1.0, 2.0, 9.9):
        assert s.step(t) == SS.SERIAL_PUSH_S
    p.room = 100000
    s.step(10.0)                     # due: the old one is cut, a new one goes
    assert s.cut == 1 and s.lines == 1
    parts = bytes(p.out).split(b"\n")
    assert len(parts) == 3 and parts[-1] == b""
    json.loads(parts[1])             # the new line is whole
    try:
        json.loads(parts[0])
        assert False, "cut line should not parse"
    except ValueError:
        pass


@case
def test_unplug_mid_line_gives_up():
    p = Port(room=50)
    s = SS.SerialStatus(p, doc, now=0.0)
    s.step(0.0)
    p.connected = False
    assert abs(s.step(0.1) - 9.9) < 1e-9       # given up, back to waiting
    assert s.cut == 1 and s.lines == 0


@case
def test_never_raises():
    def boom():
        raise MemoryError("heap")

    class Bad(Port):
        def write(self, buf):
            raise OSError(5)

    s = SS.SerialStatus(Port(), boom, now=0.0)
    assert s.step(0.0) == 10.0 and s.errors == 1 and "MemoryError" in s.last_error
    s2 = SS.SerialStatus(Bad(), doc, now=0.0)
    assert s2.step(0.0) == 10.0 and s2.errors == 1


@case
def test_too_big_dropped():
    p = Port()
    s = SS.SerialStatus(p, doc, max_b=50, now=0.0)
    s.step(0.0)
    assert p.out == b"" and s.dropped == 1


@case
def test_settings_defaults_and_clamp():
    assert SS.serial_settings({}) == (True, SS.SERIAL_PERIOD_S)
    assert SS.serial_settings({"serial_status": {"enabled": False}})[0] is False
    assert SS.serial_settings({"serial_status": {"period_s": 0.1}})[1] == SS.SERIAL_PERIOD_MIN_S
    assert SS.serial_settings({"serial_status": {"period_s": "x", "enabled": "y"}}) == (True, SS.SERIAL_PERIOD_S)
    ex = json.load(open(os.path.join(ROOT, "settings_example.json")))
    assert ex["serial_status"] == {"enabled": True, "period_s": 10}
    assert config_loader.DEFAULTS["serial_status"] == ex["serial_status"]
    cfg = config_loader.Config(config_loader._deep_merge(config_loader.DEFAULTS, {}))
    assert SS.serial_settings(cfg) == (True, 10.0)


@case
def test_open_without_usb_cdc_is_none():
    said = []
    assert SS.open_serial_status({}, doc, log=said.append) is None   # host: no usb_cdc
    assert SS.open_serial_status({"serial_status": {"enabled": False}}, doc,
                                 log=said.append) is None


@case
def test_open_with_data_port_none_or_present():
    import types
    said = []
    sys.modules["usb_cdc"] = types.SimpleNamespace(data=None)
    try:
        assert SS.open_serial_status({}, doc, log=said.append) is None
        p = Port()
        sys.modules["usb_cdc"] = types.SimpleNamespace(data=p)
        s = SS.open_serial_status({"serial_status": {"period_s": 5}}, doc, log=said.append)
        assert s is not None and s.port is p and s.period_s == 5.0
    finally:
        del sys.modules["usb_cdc"]


@case
def test_boot_enables_data_port_and_files_txt():
    boot = open(os.path.join(ROOT, "src", "boot.py")).read()
    assert "data=True" in boot
    names = open(os.path.join(ROOT, "tools", "files.txt")).read().split()
    assert "serial_status.py" in names


LONG = "x" * 200


def worst():
    """As test_ring_send.worst_hb, without farm_ws: every field the line
    keeps at its biggest (strings are cut to HB_UDP_STR_MAX anyway)."""
    d = doc()
    b = d["body"]
    b.update({"panel": [[255, 255, 255]] * 6, "panel_state": ["heartbeat_holdover"] * 6,
              "link": "wifi_only", "tower": "ringing_down",
              "net": dict(("c%d" % i, 123456) for i in range(60)),
              "wifi": {"ip": "192.168.100.100", "rssi": -100}, "sync": "unsynced",
              "time": {"srv": "192.168.100.100", "stratum": 15, "peer": {"srv": LONG, "stratum": 16},
                       "last": {"ok": True, "why": LONG, "rtt_ms": 1.23456}},
              "last_seq": 123456789, "chan": [["unknown", "w" * 80]] * 8,
              "uptime_s": 123456789, "last_reset": "BROWNOUT " + LONG, "watchdog": True,
              "edges_lost": 123456, "late_drains": 123456, "strokes_undelivered": 123456,
              "mem_free": 123456789, "gc_heap_kb": 123456})
    b["net"].update(drops=123456, last_drop_reason="send timeout " + LONG)
    d.update(seq=123456789, boot_id="7091cdab", id="sandswing-pico")
    return d


def plain_dumps(obj):
    return json.dumps(obj)                 # CircuitPython without separators


@case
def test_worst_case_line_fits_with_plain_dumps_too():
    for dumps in (link_health.dumps_compact, plain_dumps):
        p = Port()
        s = SS.SerialStatus(p, worst, dumps=dumps, now=0.0)
        s.step(0.0)
        assert s.lines == 1 and s.dropped == 0, (dumps, s.counters())
        assert len(p.out) <= SS.SERIAL_LINE_MAX_B, len(p.out)
        json.loads(bytes(p.out))


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
raise SystemExit(1 if n_fail else 0)
