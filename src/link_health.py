"""
link_health.py
==============
Pure-Python state machine for the Sand* farm link (Wi-Fi + /ws clients).

Runs on CPython (desktop tests) AND CircuitPython.
No board, wifi, httpserver, or asyncio.

Observations in, next action out. Side effects (radio reset, Pico reset,
rejoin, gc.collect, empty-socket probe) stay in concentrator.py.

States
------
    DOWN            no Wi-Fi / no IP
    UP              Wi-Fi up, clients may or may not be present
    CLIENT_LIVE     at least one heartbeat client heard recently
    CLIENT_SILENT   heartbeat client(s) silent past CLIENT_SILENT_S
    GATEWAY_FAILING gateway ping misses accumulating (quiet periods only)
    RADIO_RESET     radio has been cycled; waiting for rejoin
    DEAD            link dead long enough that Pico reset is indicated
                    (only when watchdog is armed)

Events (returned by update)
---------------------------
    wifi_lost, wifi_up, client_live, client_silent,
    gateway_fail, radio_reset, pico_reset, quiet

Public API
----------
    LinkHealth()
    update(now_s, obs) -> (state, events)
        obs is a dict of the facts the concentrator already has:
            wifi_up: bool
            n_clients: int
            hb_seen: bool
            last_rx_age_s: float or None
            last_gw_ok: bool or None
            gw_fail_count: int
            tower_inactive: bool
            quiet_ms: int          # ms since last stroke
            free_mem: int or None
            watchdog_armed: bool
"""

DOWN = "DOWN"
UP = "UP"
CLIENT_LIVE = "CLIENT_LIVE"
CLIENT_SILENT = "CLIENT_SILENT"
GATEWAY_FAILING = "GATEWAY_FAILING"
RADIO_RESET = "RADIO_RESET"
DEAD = "DEAD"

# Defaults matching the constants currently in concentrator.py
CLIENT_SILENT_S = 20.0
GATEWAY_FAIL_N = 4
LINK_DEAD_S = 120.0
LINK_DEAD_RESET_S = 600.0
MEM_QUIET_MS = 3000
RADIO_RESET_SETTLE_S = 60.0


class LinkHealth:
    def __init__(self):
        self.state = DOWN
        self._dead_since = None
        self._radio_reset_at = None
        self._latched = False       # a radio reset was asked for this silent spell
        self.events = []

    def update(self, now_s, obs):
        self.events = []
        wifi_up = bool(obs.get("wifi_up"))
        n_clients = int(obs.get("n_clients") or 0)
        hb_seen = bool(obs.get("hb_seen"))
        last_rx_age = obs.get("last_rx_age_s")
        gw_ok = obs.get("last_gw_ok")
        gw_fails = int(obs.get("gw_fail_count") or 0)
        tower_inactive = bool(obs.get("tower_inactive"))
        quiet_ms = int(obs.get("quiet_ms") or 0)
        armed = bool(obs.get("watchdog_armed"))

        # Quiet period (for GC / probes) is reported as an event but does not change state
        if quiet_ms >= MEM_QUIET_MS:
            self.events.append("quiet")

        # Wi-Fi down is always terminal for the link
        if not wifi_up:
            if self.state != DOWN:
                self.events.append("wifi_lost")
            self.state = DOWN
            self._dead_since = None
            return self.state, list(self.events)

        # Wi-Fi is up
        if self.state == DOWN:
            self.events.append("wifi_up")
            self.state = UP

        # A radio reset normally drops Wi-Fi, which returns us to DOWN.  If it did
        # not, do not stay in RADIO_RESET for ever (that blocked every later reset).
        if self.state == RADIO_RESET and self._radio_reset_at is not None \
                and now_s - self._radio_reset_at >= RADIO_RESET_SETTLE_S:
            self.state = UP

        # Client liveness
        live = n_clients > 0 and hb_seen and (
            last_rx_age is None or last_rx_age < CLIENT_SILENT_S
        )
        if hb_seen and last_rx_age is not None and last_rx_age < CLIENT_SILENT_S:
            self._latched = False   # frames are arriving again: a new spell may start
        if live:
            if self.state != CLIENT_LIVE:
                self.events.append("client_live")
            self.state = CLIENT_LIVE
            self._dead_since = None
        elif n_clients > 0 and hb_seen:
            if self.state != CLIENT_SILENT:
                self.events.append("client_silent")
            self.state = CLIENT_SILENT
            if self._dead_since is None:
                self._dead_since = now_s
        else:
            # no clients or no heartbeat yet — stay UP
            if self.state in (CLIENT_LIVE, CLIENT_SILENT):
                self.state = UP
            # Heartbeats were heard this boot but nothing has arrived for a
            # while, even with no client connected (the clients dropped, or
            # the radio is deaf): the spell began when the last frame did.
            if hb_seen and last_rx_age is not None and last_rx_age >= CLIENT_SILENT_S \
                    and self._dead_since is None:
                self._dead_since = now_s - last_rx_age

        # Gateway failures only matter while quiet / inactive (matches current policy)
        if tower_inactive and quiet_ms >= MEM_QUIET_MS and gw_ok is False:
            if gw_fails >= GATEWAY_FAIL_N and self.state not in (RADIO_RESET, DEAD):
                self.events.append("gateway_fail")
                self.events.append("radio_reset")
                self.state = RADIO_RESET
                self._radio_reset_at = now_s

        # Dead-link escalation
        if self._dead_since is not None:
            dead_for = now_s - self._dead_since
            if dead_for >= LINK_DEAD_RESET_S and armed and self.state != DEAD:
                self.events.append("pico_reset")
                self.state = DEAD
            elif dead_for >= LINK_DEAD_S and self.state not in (RADIO_RESET, DEAD) \
                    and not self._latched and tower_inactive:
                self._latched = True      # one reset per silent spell, not one per rejoin
                self.events.append("radio_reset")
                self.state = RADIO_RESET
                self._radio_reset_at = now_s

        return self.state, list(self.events)