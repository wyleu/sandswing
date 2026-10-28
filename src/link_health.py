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
    RADIO_RESET     radio has been cycled; waiting for rejoin
    DEAD            link unhealthy for PICO_RESET_S with every softer remedy
                    tried: reset the Pico (not only when the hardware watchdog
                    is armed: a wedged Wi-Fi stack never recovers by itself)

Events (returned by update)
---------------------------
    wifi_lost, wifi_up, client_live, client_silent,
    gateway_fail, radio_reset, pico_reset, quiet

A second, independent machine (SendPath, below) models the /ws send path:
SEND_OK, SEND_BACKOFF, SEND_DEGRADED.  It is table-driven: SEND_LEVELS and
SEND_TRANSITIONS are the whole policy, farm_ws.py only asks it "may I accept?
how many events do I replay, how fast, with what send budget?" and obeys.

The state name is only a label.  What stops a reset repeating are the flags
(`_latched` for silence, `_stall_reset_at` for stalls, `_radio_reset_at` for
the settle time, `_pico_asked`), because a live client frame sets the label
back to CLIENT_LIVE on the next pass.  See docs/link-health.md for the diagram.

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
            gw_failing: bool       # a gateway ping has failed and none has worked since
            tower_inactive: bool
            quiet_ms: int          # ms since last stroke
            free_mem: int or None
"""

DOWN = "DOWN"
UP = "UP"
CLIENT_LIVE = "CLIENT_LIVE"
CLIENT_SILENT = "CLIENT_SILENT"
RADIO_RESET = "RADIO_RESET"
DEAD = "DEAD"

# Defaults matching the constants currently in concentrator.py
CLIENT_SILENT_S = 20.0
GATEWAY_FAIL_N = 4
LINK_DEAD_S = 45.0
STALL_N = 3                  # this many 5 s+ polls within STALL_WINDOW_S: the radio is sick
STALL_WINDOW_S = 300.0
PICO_RESET_S = 300.0         # unhealthy this long, tower quiet: reset the Pico
CALM_S = 90.0                # Wi-Fi up with no bad sign this long: the spell is over
MEM_QUIET_MS = 3000
RADIO_RESET_SETTLE_S = 60.0


# ---------------------------------------------------------------------------
# Send path (/ws writes).  One table, no logic elsewhere.
#
# Why: CircuitPython 10.0.3 lwip_tcp_send() sleeps up to 200 x 50 ms = 10 s
# *inside one socket.send()* when lwIP's tcp_write() returns ERR_MEM (send
# queue full of unacknowledged segments, typical when 32 replay frames are
# pushed at a new client over an extender).  A Python deadline is only looked
# at after send() returns, so the only defences are (1) never queue more than
# the link takes (pace and shrink the replay), (2) notice a slow send at once
# and (3) refuse the instant reconnect that would repeat it.  See
# docs/link-health.md "Send path".
# ---------------------------------------------------------------------------
SEND_OK = "SEND_OK"                  # replay and pace as configured at level 0
SEND_BACKOFF = "SEND_BACKOFF"        # a send was slow/failed: refuse new clients (503)
SEND_DEGRADED = "SEND_DEGRADED"      # accepting again, but gentler replay until calm

# Per-poll hard budget and the bounds every blocking wait must sit under.
# ws_bounds.py and farm_ws.py take their numbers from here.
POLL_BUDGET_S = 1.0          # farm_ws.poll() total, aim; phases stop when it is spent
REQ_BOUND_S = 0.3            # the library reading one HTTP request (ws_bounds)
REPLY_BOUND_S = 0.3          # the library sending a reply / handshake / 503 (ws_bounds)
FRAME_BOUND_S = 0.3          # the rest of a started client frame (ws_bounds)
SEND_TIMEOUT_S = 0.4         # one frame to one client (replay or live)
CLOSE_TIMEOUT_S = 0.1        # a close frame to a stuck peer
MIN_SLICE_S = 0.05           # not worth starting a send with less budget than this

# A call that took this long counts as a bad send, whether or not it raised.
SLOW_S = {"replay": 0.2, "live": 0.3, "server": 1.0, "close": 0.15, "poll": 1.5}

# Level 0 is SEND_OK.  A bad send raises the level by one (capped at the last
# row); SEND_BACKOFF lasts hold_s; SEND_DEGRADED then lasts calm_s at that
# level without a bad send, after which the level falls by one, down to 0.
# calm_s is long on purpose (hours): a level that was needed once must not be
# given up again within the same ringing session, or a link that needs it
# would flap between levels and stall once per calm_s.
#   level: (hold_s, replay_n, chunk_frames, chunk_gap_s, calm_s)
# replay_n events are replayed to a new client, chunk_frames per poll turn,
# at least chunk_gap_s between turns (about 0.5 kB per frame, so level 0 is
# ~5 kB/s into the TCP queue, level 1 ~2 kB/s, level 4 ~0.5 kB/s, level 5 none).
SEND_LEVELS = (
    (0.0,  32, 1, 0.10,    0.0),
    (2.0,  32, 1, 0.25, 7200.0),
    (4.0,  16, 1, 0.50, 7200.0),
    (8.0,   8, 1, 0.50, 3600.0),
    (16.0,  4, 1, 1.00, 1800.0),
    (30.0,  0, 0, 1.00, 1800.0),     # cap: hold 30 s, no replay at all
)
SEND_MAX_LEVEL = len(SEND_LEVELS) - 1

# (state, event) -> (next state, level change).  A pair not listed changes
# nothing.  Events: bad (slow or failed send), hold_over (hold_s elapsed),
# calm (calm_s without a bad send), level0 (calm brought the level to 0).
SEND_TRANSITIONS = {
    (SEND_OK,       "bad"):       (SEND_BACKOFF, +1),
    (SEND_DEGRADED, "bad"):       (SEND_BACKOFF, +1),
    (SEND_BACKOFF,  "bad"):       (SEND_BACKOFF, 0),    # existing clients failing: no extra escalation
    (SEND_BACKOFF,  "hold_over"): (SEND_DEGRADED, 0),
    (SEND_DEGRADED, "calm"):      (SEND_DEGRADED, -1),
    (SEND_DEGRADED, "level0"):    (SEND_OK, 0),
}
# What each state lets farm_ws do.
SEND_ACCEPTS = {SEND_OK: True, SEND_DEGRADED: True, SEND_BACKOFF: False}


class SendPlan:
    """The model's answer to farm_ws: obey it."""
    def __init__(self, state, level, accept, retry_after_s, replay_n,
                 chunk_frames, chunk_gap_s):
        self.state = state
        self.level = level
        self.accept = accept
        self.retry_after_s = retry_after_s
        self.replay_n = replay_n
        self.chunk_frames = chunk_frames
        self.chunk_gap_s = chunk_gap_s
        self.poll_s = POLL_BUDGET_S
        self.send_s = SEND_TIMEOUT_S
        self.close_s = CLOSE_TIMEOUT_S


class SendPath:
    """Table-driven /ws send-path health.  Time and observations in, plan and
    log events out; no sockets.

        plan(now)                      -> SendPlan (also runs the timed transitions)
        observe(now, kind, took_s, ok) -> True if that send counted as bad
        take_events()                  -> [(what, info)] for `link` log lines
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.state = SEND_OK
        self.level = 0
        self.hold_until = None
        self.last_bad = None
        self.level_since = None        # when the current level began (calm timer)
        self.refused = 0               # refused during the current spell
        self.bad_count = 0
        self.events = []

    def _fire(self, now, event, why=None):
        key = (self.state, event)
        if key not in SEND_TRANSITIONS:
            return
        nxt, delta = SEND_TRANSITIONS[key]
        before = self.state
        self.state = nxt
        self.level = min(SEND_MAX_LEVEL, max(0, self.level + delta))
        if delta:
            self.level_since = now
        if nxt == SEND_BACKOFF and before != SEND_BACKOFF:
            self.hold_until = now + SEND_LEVELS[self.level][0]
            self.events.append(("send_backoff", {
                "level": self.level, "hold_s": SEND_LEVELS[self.level][0],
                "replay": SEND_LEVELS[self.level][1], "why": why}))
        elif before == SEND_BACKOFF and nxt == SEND_DEGRADED:
            self.events.append(("send_degraded", {
                "level": self.level, "replay": SEND_LEVELS[self.level][1],
                "refused": self.refused}))
        elif nxt == SEND_OK:
            self.events.append(("send_ok", {"refused": self.refused}))
            self.refused = 0
        elif event == "calm":
            self.events.append(("send_calmer", {
                "level": self.level, "replay": SEND_LEVELS[self.level][1]}))
        if self.state == SEND_DEGRADED and self.level == 0:
            self._fire(now, "level0")

    def _timed(self, now):
        """Transitions that only need the clock."""
        if self.state == SEND_BACKOFF and now >= self.hold_until:
            self._fire(now, "hold_over")
        if self.state == SEND_DEGRADED:
            calm_s = SEND_LEVELS[self.level][4]
            since = max(self.last_bad or 0.0, self.level_since or 0.0,
                        self.hold_until or 0.0)
            if now - since >= calm_s:
                self._fire(now, "calm")

    def plan(self, now):
        self._timed(now)
        hold, replay_n, frames, gap, _ = SEND_LEVELS[self.level]
        accept = SEND_ACCEPTS[self.state]
        retry = 0.0 if accept else max(1.0, round(self.hold_until - now, 1))
        return SendPlan(self.state, self.level, accept, retry, replay_n, frames, gap)

    def observe(self, now, kind, took_s, ok=True):
        """One send/serve call finished (or failed) after took_s.  True if bad."""
        self._timed(now)
        bad = (not ok) or took_s >= SLOW_S.get(kind, SEND_TIMEOUT_S)
        if bad:
            self.bad_count += 1
            self.last_bad = now
            self._fire(now, "bad", "%s %s %.2f s" % (
                kind, "slow" if ok else "failed", took_s))
        return bad

    def note_refused(self):
        self.refused += 1

    def take_events(self):
        out, self.events = self.events, []
        return out


class LinkHealth:
    def __init__(self):
        self.state = DOWN
        self._dead_since = None
        self._radio_reset_at = None
        self._latched = False       # a radio reset was asked for this silent spell
        self._bad_since = None      # start of the current unhealthy spell (any bad sign)
        self._calm_since = None
        self._pico_asked = False
        self._stall_reset_at = None  # last radio reset asked for because of stalls
        self.events = []

    def _spell(self, now_s, bad, good, tower_inactive):
        """Track one unhealthy spell across radio resets and rejoins, and say
        True once, when it has lasted PICO_RESET_S with the tower quiet.
        bad: Wi-Fi down, gateway failing, or a client gone silent.
        good: a gateway ping worked or a client is live: the spell is over.
        Wi-Fi up with no bad sign for CALM_S also ends it, so a blip with a
        router that ignores pings (and no client) never causes a reset."""
        if good:
            self._bad_since = self._calm_since = None
            return False
        if bad:
            self._calm_since = None
            if self._bad_since is None:
                self._bad_since = now_s
        elif self._calm_since is None:
            self._calm_since = now_s
        elif now_s - self._calm_since >= CALM_S:
            self._bad_since = None
        if self._bad_since is None or not tower_inactive or self._pico_asked:
            return False
        if now_s - self._bad_since >= PICO_RESET_S:
            self._pico_asked = True
            return True
        return False

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
        gw_failing = bool(obs.get("gw_failing"))
        stalled = int(obs.get("stalls") or 0) >= STALL_N

        # Quiet period (for GC / probes) is reported as an event but does not change state
        if quiet_ms >= MEM_QUIET_MS:
            self.events.append("quiet")

        # Wi-Fi down is always terminal for the link
        if not wifi_up:
            if self.state != DOWN:
                self.events.append("wifi_lost")
            self.state = DOWN
            self._dead_since = None
            if self._spell(now_s, True, False, tower_inactive):
                self.events.append("pico_reset")
                self.state = DEAD
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
            if dead_for >= LINK_DEAD_S and self.state not in (RADIO_RESET, DEAD) \
                    and not self._latched and tower_inactive:
                self._latched = True      # one reset per silent spell, not one per rejoin
                self.events.append("radio_reset")
                self.state = RADIO_RESET
                self._radio_reset_at = now_s

        # Clients that keep reconnecting hide a sick radio: frames arrive, but
        # sends keep stalling for 10 s.  Repeated stalls ask for a radio reset.
        if stalled and tower_inactive and quiet_ms >= MEM_QUIET_MS \
                and self.state not in (RADIO_RESET, DEAD) \
                and (self._stall_reset_at is None
                     or now_s - self._stall_reset_at >= RADIO_RESET_SETTLE_S):
            self._stall_reset_at = now_s
            self.events.append("radio_reset")
            self.state = RADIO_RESET
            self._radio_reset_at = now_s

        silent = self._dead_since is not None
        good = ((self.state == CLIENT_LIVE) or (gw_ok is True and not silent)) \
            and not stalled
        if self._spell(now_s, gw_failing or silent or stalled, good, tower_inactive):
            self.events.append("pico_reset")
            self.state = DEAD
        return self.state, list(self.events)