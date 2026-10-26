# tests for the farm page's left-hand panels (sand_status.from_concentrator)
from support import *   # fakes, rigs, case/expect, finish
import sand_status


def sec(doc, sid):
    return dict((s["id"], s) for s in doc["sections"])[sid]


def val(doc, sid, key):
    return dict((i["key"], i) for i in sec(doc, sid)["items"])[key]


MORE = {
    "rssi": -78,
    "link": {"wifi_lost": 5, "rejoins": 5, "radio_resets": 0, "clients": 1,
             "live": 1, "drops": 5, "last_drop_reason": "peer silent 20 s",
             "last_drop_age_s": 300},
    "time": {"state": "synced", "srv": "192.168.0.221", "stratum": 1,
             "age_s": 42, "peer": {"srv": "192.168.0.221", "stratum": 1, "leap": 0},
             "last": {"ok": True}},
    "chans": [{"kind": "lb", "why": None, "n": 0, "ago_s": None},
              {"kind": "trigger", "why": None, "n": 12, "ago_s": 130},
              {"kind": "fault", "why": "Orange open", "n": 0, "ago_s": None}],
    "uptime_s": 7500, "last_reset": "WATCHDOG", "watchdog": True,
    "boot_id": "ab12cd34", "seq": 77, "mem_free": 131072, "build": "6d6d5de",
}
FACTS = {"ip": "192.168.0.81", "ssid": "Home", "heads": ["lb", "trigger", "fault"],
         "tower": "inactive", "more": MORE}


@case
def test_old_facts_still_give_the_three_panels():
    doc = sand_status.from_concentrator({"ip": "1.2.3.4", "heads": ["lb"], "tower": "inactive"})
    assert [s["id"] for s in doc["sections"]] == ["network", "heads", "tower"]


@case
def test_network_shows_signal_drops_and_last_drop():
    doc = sand_status.from_concentrator(FACTS)
    assert val(doc, "network", "rssi")["value"] == "-78 dBm weak"
    assert val(doc, "network", "rssi")["level"] == "error"
    assert val(doc, "network", "wifi_lost")["value"] == "5 times"
    assert val(doc, "network", "drop")["value"] == "peer silent 20 s, 5 min ago"
    assert sec(doc, "network")["level"] == "error"


@case
def test_time_panel():
    doc = sand_status.from_concentrator(FACTS)
    assert val(doc, "time", "source")["value"] == "192.168.0.221 stratum 1"
    assert val(doc, "time", "age")["value"] == "42 s ago"
    stale = dict(MORE, time={"state": "stale", "age_s": 1200, "peer": {"leap": 3}})
    doc = sand_status.from_concentrator(dict(FACTS, more=stale))
    assert sec(doc, "time")["level"] == "warn"
    assert val(doc, "time", "leap")["level"] == "warn"


@case
def test_heads_panel_strokes_and_fault_why():
    doc = sand_status.from_concentrator(FACTS)
    assert val(doc, "heads", "ch2")["value"] == "trigger \u00b7 12 strokes, last 2 min ago"
    assert val(doc, "heads", "ch3")["level"] == "error"
    assert val(doc, "heads", "ch3_why")["value"] == "Orange open"


@case
def test_concentrator_panel():
    doc = sand_status.from_concentrator(FACTS)
    assert val(doc, "concentrator", "uptime")["value"] == "2 h 5 min"
    assert val(doc, "concentrator", "reset")["level"] == "warn"
    assert val(doc, "concentrator", "mem")["value"] == "128 kB"
    assert val(doc, "concentrator", "boot")["value"] == "ab12cd34 / 77"
    assert val(doc, "concentrator", "build")["value"] == "6d6d5de"


@case
def test_build_unknown_when_not_stamped():
    facts = dict(FACTS, more=dict(MORE, build=None))
    doc = sand_status.from_concentrator(facts)
    assert val(doc, "concentrator", "build")["value"] == "unknown"


@case
def test_missing_bits_do_not_break():
    doc = sand_status.from_concentrator(dict(FACTS, more={"time": {}, "link": {}}))
    assert val(doc, "network", "gateway")["value"] == "—"
    assert val(doc, "time", "sync")["value"] == "unsynced"


@case
def test_no_rssi_shows_gateway_ping():
    def doc_with(**link):
        more = dict(MORE)
        more["rssi"] = None
        more["link"] = dict(more.get("link") or {}, **link)
        return sand_status.from_concentrator(dict(FACTS, more=more))
    d = doc_with(gw_ms=4.2, gw_fail=0)
    assert val(d, "network", "gateway")["value"] == "4.2 ms"
    assert val(d, "network", "gateway")["level"] == "ok"
    d = doc_with(gw_ms=None, gw_fail=3)
    assert val(d, "network", "gateway")["value"] == "no reply (3 missed)"
    assert val(d, "network", "gateway")["level"] == "error"


@case
def test_top_bar_summary_is_the_last_log_line():
    d = sand_status.from_concentrator(dict(FACTS, more=dict(MORE, last_line="1137.074 ws poll took 31.0 s")))
    assert d["summary"] == "1137.074 ws poll took 31.0 s", d["summary"]
    d = sand_status.from_concentrator(dict(FACTS, more=dict(MORE)))   # nothing said yet
    assert d["summary"].startswith("heads "), d["summary"]


@case
def test_lastline_remembers_lines_but_not_the_memory_chatter():
    import io, contextlib
    import lastline
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lastline.print("1.0 link wifi_back")
        lastline.print("mem free", 129, "kB")
        lastline.print("x" * 300)
    assert buf.getvalue().splitlines()[1] == "mem free 129 kB"      # still printed
    assert lastline.get() == "x" * lastline.MAX_LEN
    with contextlib.redirect_stdout(buf):
        lastline.print("2.0 link client_dropped")
        lastline.print("mem free 126 kB")
    assert lastline.get() == "2.0 link client_dropped"


finish()
