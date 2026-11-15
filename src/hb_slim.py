"""
hb_slim.py
==========
The slim heartbeat: the fields Sandbells reads, strings cut and floats
rounded (udp_heartbeat_doc), and compact JSON (dumps_compact).  Moved out of
link_health.py so serial_status.py and tune.py (the USB data port, ring_send
"off") need no Wi-Fi module; link_health imports every name back, and its
udp_heartbeat() (the datagram size fit, HB_UDP_DROP) builds on these.

No board, wifi or socket imports: runs on CPython (tests) and CircuitPython.
"""

# The UDP heartbeat (ring_send "udp"): one datagram, never fragmented.  The
# WebSocket heartbeat (heartbeat_doc) carries body.net = every farm_ws
# counter (~60 keys, ~1.2 kB) and full sntp/heap detail: ~2.4 kB on the
# Pico, so every one was too_big.  udp_heartbeat() sends what Sandbells
# reads (ingest Ingest._panel_frame + LinkState.heartbeat: schema, type,
# src, id, boot_id, seq, body.uid; link_widget.js: body.panel, panel_state,
# chan, sync, time.srv/peer/last.ok/last.why, wifi.rssi, net.drops,
# net.last_drop_reason) plus link, tower, last_seq, uptime_s, last_reset;
# the counters are in the console's "udp stats" line.  Every string cut to
# HB_UDP_STR_MAX, compact separators where json.dumps takes them.  Still too
# big: HB_UDP_DROP, in order, until it fits; anything that cannot is
# udp_send's too_big.  Same schema and type: Sandbells needs no change.
HB_UDP_STR_MAX = 40                 # any string in the UDP heartbeat is cut to this many chars
HB_UDP_FLOAT_DP = 1                 # ...and any float rounded to this many places
HB_UDP_ENVELOPE = ("schema", "type", "src", "id", "boot_id", "seq")
HB_UDP_BODY = ("panel", "panel_state", "sync", "uid", "chan", "link", "tower",
               "last_seq", "uptime_s", "last_reset")
HB_UDP_NET = ("drops", "last_drop_reason")
HB_UDP_WIFI = ("ip", "rssi")
HB_UDP_TIME = ("srv", "stratum", "peer")
HB_UDP_TIME_LAST = ("ok", "why")
HB_UDP_CHAN_WHY = ("body", "chan", "why")    # pseudo path: every chan why -> None


def dumps_compact(obj):
    """json.dumps without spaces where CircuitPython's json takes
    separators (MICROPY_PY_JSON_SEPARATORS), else the plain one."""
    import json
    try:
        return json.dumps(obj, separators=(",", ":"))
    except TypeError:
        return json.dumps(obj)


def _slim(v):
    """Strings cut to HB_UDP_STR_MAX, floats rounded, lists/dicts walked."""
    if isinstance(v, str):
        return v[:HB_UDP_STR_MAX]
    if isinstance(v, float):
        return round(v, HB_UDP_FLOAT_DP)
    if isinstance(v, (list, tuple)):
        return [_slim(x) for x in v]
    if isinstance(v, dict):
        return dict((k, _slim(x)) for k, x in v.items())
    return v


def _pick(d, keys):
    d = d if isinstance(d, dict) else {}
    return dict((k, _slim(d[k])) for k in keys if k in d)


def udp_heartbeat_doc(doc):
    """The slim copy of a heartbeat_doc() (doc is not changed)."""
    out = _pick(doc, HB_UDP_ENVELOPE)
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    b = _pick(body, HB_UDP_BODY)
    b["net"] = _pick(body.get("net"), HB_UDP_NET)
    b["wifi"] = _pick(body.get("wifi"), HB_UDP_WIFI)
    t = body.get("time")
    if isinstance(t, dict):
        b["time"] = _pick(t, HB_UDP_TIME)
        if isinstance(t.get("last"), dict):
            b["time"]["last"] = _pick(t["last"], HB_UDP_TIME_LAST)
    else:
        b["time"] = None
    out["body"] = b
    return out
