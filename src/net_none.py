"""
net_none.py
===========
farm_ws's stand-in when settings.json link.ring_send is "off": no Wi-Fi,
no server, no datagrams for the whole boot.  concentrator.py imports this
as farm_ws on "off", so farm_ws.py, adafruit_httpserver,
adafruit_connection_manager, link_health, ws_bounds and status_doc are never
loaded (about 90 kB of RAM on the Pico, measured as CircuitPython 10.0.3
.mpy sizes) and the startup import has room.

Every farm_ws name concentrator.py uses is here (tests/test_net_none.py
checks that), each answering as farm_ws does on "off": nothing sent,
nothing kept, no Wi-Fi, no clients.  One thing is still tracked, as
farm_ws's NetMode does: "ringing" = a stroke within link.ring_quiet_s.
No board, wifi or socket imports.
"""

import time

from ring_mode import ring_settings, RING_QUIET_S

# the heartbeat's "net" (hb_slim.HB_UDP_NET) and the keys concentrator reads
stats = {"drops": 0, "last_drop_reason": None, "hb_seen": False, "mem_low": 0}
_ring = {"quiet_s": RING_QUIET_S, "last_stroke": None}
STROKE_TYPE = "stroke"          # sand_event type of a stroke (farm_ws.send's test)


def configure_ring(cfg):
    """As farm_ws.configure_ring: (mode, batch_s, quiet_s); only quiet_s is used."""
    mode, batch_s, quiet_s = ring_settings(cfg)
    _ring["quiet_s"] = quiet_s
    return mode, batch_s, quiet_s


def configure_udp(cfg):
    return None


# ---- the mode: always "off" ------------------------------------------------
def net_off():
    return True


def net_udp():
    return False


def net_tcp():
    return False


def net_rejoins():
    return False


def net_ringing(now=None):
    last = _ring["last_stroke"]
    if last is None:
        return False
    now = time.monotonic() if now is None else now
    return now - last < _ring["quiet_s"]


# ---- sending: nothing goes anywhere -----------------------------------------
def send(obj, keep=True, udp_fit=None):
    """Nothing is built or kept (as farm_ws.send on "off"); a stroke still
    marks the bells ringing.  -> False (not sent)."""
    if keep and isinstance(obj, dict) and obj.get("type") == STROKE_TYPE:
        _ring["last_stroke"] = time.monotonic()
    return False


def poll():
    pass


def memory_low(where, err=None):
    stats["mem_low"] += 1
    try:
        import gc
        gc.collect()
    except ImportError:
        pass


def undelivered_strokes():
    return []


def strokes_kept():
    return 0


def take_link_notes():
    return []


def link_brief(keys):
    return {k: stats.get(k) for k in keys}


def link_status():
    return dict(stats)


def set_tower(tower, now=None):
    return None


def set_rx_hook(fn):
    pass


def drop_all(why):
    pass


# ---- Wi-Fi: none --------------------------------------------------------------
def connect_wifi(cfg, timeout=None):
    return False


def wifi_ok():
    return False


def wifi_ip():
    return None


def wifi_rssi():
    return None


def wifi_ssid():
    return None


def reset_radio():
    pass


def radio_off_due():
    return False


def radio_off():
    pass


# ---- server and clients: none ---------------------------------------------------
def start_server(cfg, payload_fn):
    return None


def server_running():
    return False


def server_host():
    return None


def stop_server():
    pass


def ws_clients():
    return 0


def live_clients(now=None):
    return 0


def sandbells_connected():
    return False


def last_rx_age_s(now=None):
    return None


def stalls_since(now, window_s):
    return 0


def _device_uid():
    """The Pico's UID (hex), as farm_ws: the heartbeat's body.uid."""
    try:
        import microcontroller
        return "".join("%02X" % b for b in microcontroller.cpu.uid)
    except Exception:
        return None
