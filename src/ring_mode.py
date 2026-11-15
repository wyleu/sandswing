"""
ring_mode.py
============
link.ring_send (the network while the bells ring) and the numbers
ring_settings() needs, in one small module.

concentrator.py reads the mode from here BEFORE it imports any network
module: with ring_send "off" (no Wi-Fi this boot) the Wi-Fi modules
(farm_ws, its web server library, link_health, ws_bounds, status_doc,
sand_status) are never imported; net_none.py stands in for farm_ws.
link_health.py imports these names back, so its tables and every caller
see the same values (one place).  The modes are described in
link_health.py, above NET_PLAN.

No board, wifi or socket imports: runs on CPython (tests) and CircuitPython.
"""

RING_SEND_LIVE = "live"
RING_SEND_BATCHED = "batched"
RING_SEND_SHED = "shed"
RING_SEND_OFF = "off"
RING_SEND_UDP = "udp"
RING_SEND_MODES = (RING_SEND_LIVE, RING_SEND_BATCHED, RING_SEND_SHED, RING_SEND_OFF,
                   RING_SEND_UDP)
RING_SEND = RING_SEND_LIVE          # default: behaviour as before this table

MS_PER_S = 1000
MIN_SLICE_S = 0.05           # not worth starting a send with less budget than this

# A minute bell tolls once per 60 s at the slowest; twice that bridges one
# missed or late stroke without the network coming back between tolls.
SLOWEST_TOLL_S = 60.0
RING_QUIET_S = 2 * SLOWEST_TOLL_S   # link.ring_quiet_s
# One batch per client per BATCH_PERIOD_S (link_health: why 1 s).
BATCH_PERIOD_S = 1.0                # link.batch_ms


def ring_settings(cfg):
    """settings.json link.ring_send / batch_ms / ring_quiet_s ->
    (mode, batch_s, quiet_s); anything unusable gives the default."""
    sec = (cfg.get("link", {}) if cfg is not None else {}) or {}
    mode = sec.get("ring_send", RING_SEND)
    if mode not in RING_SEND_MODES:
        mode = RING_SEND
    try:
        batch_s = max(MIN_SLICE_S, float(sec.get("batch_ms", BATCH_PERIOD_S * MS_PER_S)) / MS_PER_S)
    except (TypeError, ValueError):
        batch_s = BATCH_PERIOD_S
    try:
        quiet_s = max(0.0, float(sec.get("ring_quiet_s", RING_QUIET_S)))
    except (TypeError, ValueError):
        quiet_s = RING_QUIET_S
    return mode, batch_s, quiet_s


def net_off(cfg):
    """True when settings.json says link.ring_send "off" (no Wi-Fi this boot)."""
    return ring_settings(cfg)[0] == RING_SEND_OFF
