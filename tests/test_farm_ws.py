# tests for farm_ws.py (several /ws clients; fake network modules)
import json
import sys
import types

from support import case, finish

# farm_ws imports wifi / adafruit_connection_manager / adafruit_httpserver at
# module level; give it inert fakes (no network in the harness).
for name in ("wifi", "adafruit_connection_manager"):
    sys.modules.setdefault(name, types.ModuleType(name))
_http = types.ModuleType("adafruit_httpserver")


class FakeWs:
    """Just the Websocket surface farm_ws uses (adafruit_httpserver 4.8.2)."""
    def __init__(self, request=None):
        self.closed = False
        self.sent = []
        self.close_code = None
        self.on_receive = None      # None | "close" | Exception
        self.send_error = None

    def receive(self, fail_silently=False):
        if isinstance(self.on_receive, Exception):
            raise self.on_receive
        if self.on_receive == "close":   # library answers a client close
            self.closed = True
        if isinstance(self.on_receive, list) and self.on_receive:
            return self.on_receive.pop(0)  # text frames from the client
        return None

    def send_message(self, msg, opcode=None, fail_silently=False):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(msg)

    def close(self, code=None, reason=None):
        if not self.closed:
            self.close_code = code
        self.closed = True


for attr in ("Server", "Request", "Response", "GET"):
    setattr(_http, attr, object)
_http.Websocket = FakeWs
sys.modules["adafruit_httpserver"] = _http

import farm_ws
import link_health
import status_doc


def fresh(n):
    farm_ws.reset_state()
    return [farm_ws._accept(FakeWs()) for _ in range(n)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeConn:
    """socketpool socket, non-blocking: send() takes at most `take` bytes,
    or raises EAGAIN (TCP buffer full) when take == 0."""
    def __init__(self, take=4096):
        self.take = take
        self.data = bytearray()

    def send(self, view):
        if self.take == 0:
            raise OSError(farm_ws.EAGAIN, "EAGAIN")
        n = min(self.take, len(view))
        self.data.extend(bytes(view[:n]))
        return n


class RealishWs(FakeWs):
    """Has the 4.8.2 internals _send_text uses (_prepare_frame, _request)."""
    def __init__(self, conn):
        FakeWs.__init__(self)
        self._request = types.SimpleNamespace(connection=conn,
                                              client_address=("10.42.0.50", 1))

    @staticmethod
    def _prepare_frame(opcode, message):
        if len(message) < 126:
            return bytearray([0x80 | opcode, len(message)]) + message
        return bytearray([0x80 | opcode, 126]) + len(message).to_bytes(2, "big") + message


@case
def test_every_client_gets_every_send():
    a, b = fresh(2)
    assert farm_ws.send({"type": "stroke"})
    assert len(a.sent) == len(b.sent) == 1 and a.sent == b.sent
    assert farm_ws.ws_clients() == 2 and farm_ws.ws_connected()


@case
def test_one_client_closing_keeps_the_other():
    a, b = fresh(2)
    a.on_receive = "close"
    farm_ws.poll()
    assert farm_ws._clients == [b], farm_ws._clients
    assert farm_ws.send({"n": 1}) and len(b.sent) == 1 and not a.sent


@case
def test_failing_send_drops_only_that_client():
    a, b, c = fresh(3)
    b.send_error = OSError(32, "EPIPE")
    assert farm_ws.send({"n": 1})
    assert farm_ws._clients == [a, c]
    assert b.closed and b.close_code == farm_ws.WS_GOING_AWAY
    assert len(a.sent) == len(c.sent) == 1


@case
def test_receive_error_drops_only_that_client():
    a, b = fresh(2)
    a.on_receive = OSError(104, "ECONNRESET")
    farm_ws.poll()
    assert farm_ws._clients == [b]


@case
def test_over_max_refuses_the_new_client_with_1013():
    old = fresh(farm_ws.MAX_WS_CLIENTS)
    new = farm_ws._accept(FakeWs())
    assert farm_ws._clients == old and new not in farm_ws._clients
    farm_ws.poll()                           # handshake done -> polite close
    assert new.closed and new.close_code == farm_ws.WS_TRY_AGAIN_LATER
    assert all(not ws.closed for ws in old)  # long-lived clients kept
    assert farm_ws.send({"n": 1}) and not new.sent


@case
def test_no_clients_send_false():
    fresh(0)
    assert farm_ws.send({"n": 1}) is False and not farm_ws.ws_connected()


@case
def test_heartbeat_client_silent_too_long_is_dropped_and_noted():
    a, b = fresh(2)
    clock = Clock()
    real = farm_ws.time.monotonic
    farm_ws.time.monotonic = clock
    try:
        a.on_receive = ['{"type":"heartbeat"}']
        farm_ws.poll()                          # a is now a heartbeat client
        assert farm_ws.live_clients() == 1 and farm_ws.sandbells_connected()
        clock.t += farm_ws.CLIENT_SILENT_S + 1
        farm_ws.poll()
        assert farm_ws._clients == [b], farm_ws._clients   # b never sent: kept
        assert not farm_ws.sandbells_connected()   # b alone is not "Sandbells"
        notes = farm_ws.take_link_notes()
        whats = [w for w, _ in notes]
        assert whats.count("client_connected") == 2, whats
        assert whats[-1] == "client_dropped" and "silent" in notes[-1][1]["reason"]
        st = farm_ws.link_status()
        assert st["drops"] == 1 and st["silent_drops"] == 1 and st["connects"] == 2
        assert "silent" in st["last_drop_reason"]
    finally:
        farm_ws.time.monotonic = real


@case
def test_legacy_clients_still_green_until_a_heartbeat_is_seen():
    fresh(1)
    farm_ws.poll()
    assert farm_ws.sandbells_connected()        # old ingest: any client counts


@case
def test_send_to_a_full_tcp_buffer_gives_up_and_drops_that_client():
    farm_ws.reset_state()
    stuck = farm_ws._accept(RealishWs(FakeConn(take=0)))
    good_conn = FakeConn(take=7)                 # partial sends are fine
    good = farm_ws._accept(RealishWs(good_conn))
    old = farm_ws.SEND_TIMEOUT_S
    farm_ws.SEND_TIMEOUT_S = 0.05
    try:
        assert farm_ws.send({"type": "stroke", "n": 1})
    finally:
        farm_ws.SEND_TIMEOUT_S = old
    assert farm_ws._clients == [good] and stuck.closed
    assert farm_ws.stats["send_timeouts"] == 1
    assert good_conn.data[2:] == b'{"type": "stroke", "n": 1}', good_conn.data


def run_polls(clock, seconds, step=0.05):
    """poll() every `step` s of fake time (the 50 ms ws_poll_task cadence)."""
    end = clock.t + seconds
    while clock.t < end:
        farm_ws.poll()
        clock.t += step


class fake_clock:
    """Replace farm_ws's clock (and ws_bounds', which the library waits use)."""
    def __init__(self, step=0.0):
        self.step = step                 # >0: every look at the clock costs this much

    def __enter__(self):
        self.clock = TickClock(self.step) if self.step else Clock()
        self.real = farm_ws.time.monotonic
        farm_ws.time.monotonic = self.clock
        return self.clock

    def __exit__(self, *a):
        farm_ws.time.monotonic = self.real


@case
def test_new_client_gets_recent_events_replayed_not_heartbeats():
    fresh(0)
    for n in range(farm_ws.REPLAY_N + 5):
        farm_ws.send({"seq": n + 1})              # nobody connected: kept
    farm_ws.send({"heartbeat": 1}, keep=False)
    a = farm_ws._accept(FakeWs())
    assert not a.sent                            # handshake first
    with fake_clock() as clock:
        run_polls(clock, 10.0)                   # the replay is paced: a frame per turn
    assert len(a.sent) == farm_ws.REPLAY_N, len(a.sent)
    assert a.sent[0] == '{"seq": 6}' and a.sent[-1] == '{"seq": %d}' % (farm_ws.REPLAY_N + 5)


@case
def test_refusal_is_counted_and_noted():
    fresh(farm_ws.MAX_WS_CLIENTS)
    farm_ws._accept(FakeWs())
    farm_ws.poll()
    whats = [w for w, _ in farm_ws.take_link_notes()]
    assert "client_refused" in whats and farm_ws.stats["refused"] == 1


@case
def test_slow_poll_says_where_the_time_went():
    import io, contextlib
    fresh(1)
    farm_ws.send({"seq": 1})
    t = [100.0]
    class Srv:
        def poll(self):
            t[0] += 0.4                         # the library holds the loop 0.4 s
    saved = (farm_ws.time.monotonic, farm_ws._server, farm_ws._send_text, farm_ws._receive)
    farm_ws.time.monotonic = lambda: t[0]
    farm_ws._server = Srv()
    farm_ws._send_text = lambda ws, msg, *a, **k: t.__setitem__(0, t[0] + 0.1) or False  # replay 0.1 s
    def slow_read(ws, now):
        t[0] += 34.0                            # a read that overruns everything (a bug)
    farm_ws._receive = slow_read
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            farm_ws.poll()
    finally:
        farm_ws.time.monotonic, farm_ws._server, farm_ws._send_text, farm_ws._receive = saved
    out = buf.getvalue()
    assert "ws poll took 34.5 s: server 0.4, replay 0.1, read 34.0" in out, out
    assert farm_ws._path.state == "SEND_BACKOFF"          # and the model was told


@case
def test_a_slow_poll_is_counted_as_a_stall_for_link_health():
    import io, contextlib
    farm_ws.reset_state()
    t = [100.0]
    class Srv:
        def poll(self):
            t[0] += 10.0
    saved = (farm_ws.time.monotonic, farm_ws._server)
    farm_ws.time.monotonic = lambda: t[0]
    farm_ws._server = Srv()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            farm_ws.poll()
            t[0] += link_health.DRAIN_S                  # SEND_DRAIN skips the server meanwhile
            farm_ws.poll()
        assert farm_ws.stalls_since(t[0], 60.0) == 2
        assert farm_ws.stalls_since(t[0] + 100.0, 60.0) == 0     # old ones age out
    finally:
        farm_ws.time.monotonic, farm_ws._server = saved
    farm_ws.reset_state()
    assert farm_ws.stalls_since(0.0, 60.0) == 0


@case
def test_static_ip_replaces_the_dhcp_address_and_keeps_the_rest():
    import io, contextlib
    class R:
        ipv4_address = "192.168.0.253"
        ipv4_subnet = "255.255.255.0"
        ipv4_gateway = "192.168.0.1"
        ipv4_dns = "192.168.0.1"
        def set_ipv4_address(self, ipv4, netmask, gateway, ipv4_dns=None):
            self.got = (str(ipv4), netmask, gateway, ipv4_dns)
            self.ipv4_address = str(ipv4)
    r = R()
    with contextlib.redirect_stdout(io.StringIO()):
        assert farm_ws.apply_static_ip(r, "192.168.0.81") == "192.168.0.81"
    assert r.got == ("192.168.0.81", "255.255.255.0", "192.168.0.1", "192.168.0.1"), r.got
    r2 = R()                                   # no static_ip configured: DHCP stays
    assert farm_ws.apply_static_ip(r2, None) == "192.168.0.253" and not hasattr(r2, "got")
    r4 = R()                                   # DHCP already agrees: still set it, so DHCP stops
    r4.ipv4_address = "192.168.0.81"
    with contextlib.redirect_stdout(io.StringIO()):
        assert farm_ws.apply_static_ip(r4, "192.168.0.81") == "192.168.0.81"
    assert hasattr(r4, "got"), "the address must be set even when DHCP gave the same one"
    r3 = R()
    r3.set_ipv4_address = lambda **k: (_ for _ in ()).throw(OSError("no"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert farm_ws.apply_static_ip(r3, "192.168.0.81") == "192.168.0.253"   # failure: keep DHCP


@case
def test_replay_to_a_slow_client_gives_up_in_bounded_time():
    farm_ws.reset_state()
    for n in range(10):
        farm_ws.send({"seq": n})
    a = farm_ws._accept(FakeWs())
    sent = []
    a_send = farm_ws._send_text
    def slow_send(ws, msg, timeout_s=None, kind="live"):
        sent.append(msg)
        farm_ws.time.monotonic.t += 0.7      # each frame takes 0.7 s
        return farm_ws._path.observe(farm_ws.time.monotonic(), kind, 0.7, True)
    farm_ws._send_text = slow_send
    try:
        with fake_clock() as clock:
            t0 = clock.t
            farm_ws.poll()
            assert clock.t - t0 < 1.0 + 0.7, clock.t - t0   # one frame, then dropped
    finally:
        farm_ws._send_text = a_send
    assert len(sent) == 1, len(sent)         # gave up after the first slow frame
    assert a not in farm_ws._clients         # and dropped the client
    assert farm_ws._path.state == "SEND_BACKOFF"


# --- the 2026-10-28 12:51-12:53 failure, on the model -----------------------

class StubServer:
    def __init__(self, clock, cost):
        self.clock, self.cost = clock, cost

    def poll(self):
        self.clock.t += self.cost


class TickClock(Clock):
    """Every look at the clock costs `step` s: a busy-wait loop (EAGAIN) burns time."""
    def __init__(self, step=0.01):
        Clock.__init__(self)
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class LwipConn:
    """The part of lwIP that bit us.  A TCP queue of `cap` bytes drains at
    `drain_bps`.  send() into a full queue sleeps 10 s inside the call (what
    CircuitPython 10.0.3 lwip_tcp_send does when tcp_write returns ERR_MEM:
    200 x 50 ms) and then succeeds if the queue has drained.  `clock` is a
    Clock whose .t advances by that sleep; no Python deadline can interrupt it."""
    stalls = []

    def __init__(self, clock, cap=3000, drain_bps=1000.0):
        self.clock, self.cap, self.drain = clock, cap, drain_bps
        self.queued, self.last = 0.0, clock.t
        self.data = bytearray()

    def _drain(self):
        now = self.clock.t
        self.queued = max(0.0, self.queued - (now - self.last) * self.drain)
        self.last = now

    def send(self, view):
        self._drain()
        if self.queued + len(view) > self.cap:
            LwipConn.stalls.append(self.clock.t)
            self.clock.t += 10.0
            self._drain()
        self.queued += len(view)
        self.data.extend(bytes(view))
        return len(view)


def big(n):
    return {"seq": n, "type": "stroke", "pad": "x" * 480}      # ~0.5 kB like an event


@case
def test_poll_never_exceeds_its_budget_when_every_peer_is_stuck():
    farm_ws.reset_state()
    for n in range(farm_ws.REPLAY_N):
        farm_ws.send(big(n))
    for _ in range(farm_ws.MAX_WS_CLIENTS):
        farm_ws._accept(RealishWs(FakeConn(take=0)))        # TCP buffer full, never drains
    real = farm_ws.time.monotonic
    clock = TickClock(0.001)
    farm_ws.time.monotonic = clock
    farm_ws._server = StubServer(clock, 0.55)              # the library takes its 0.3 + 0.3 bounds
    worst = 0.0
    try:
        for _ in range(40):
            t0 = clock.t
            farm_ws.poll()
            worst = max(worst, clock.t - t0)
            clock.t += 0.05
            if not farm_ws._clients:
                farm_ws._accept(RealishWs(FakeConn(take=0)))
    finally:
        farm_ws.time.monotonic = real
        farm_ws._server = None
    assert worst <= link_health.POLL_BUDGET_S + 0.05, worst


@case
def test_refusal_close_and_replay_together_stay_inside_the_budget():
    farm_ws.reset_state()
    for n in range(8):
        farm_ws.send(big(n))
    old = [farm_ws._accept(RealishWs(FakeConn(take=0))) for _ in range(farm_ws.MAX_WS_CLIENTS)]
    for _ in range(3):
        farm_ws._refused.append(RealishWs(FakeConn(take=0)))    # three over the limit
    real = farm_ws.time.monotonic
    clock = TickClock(0.001)
    farm_ws.time.monotonic = clock
    try:
        t0 = clock.t
        farm_ws.poll()
        took = clock.t - t0
    finally:
        farm_ws.time.monotonic = real
    assert took <= link_health.POLL_BUDGET_S + 0.05, took
    assert farm_ws._refused or all(ws.closed for ws in old if ws not in farm_ws._clients)


# --- the 2026-10-29 14:26-14:30 failure: per-client model (ClientPath) -------

DISPLAY = "192.168.0.125"


def peer_ws(conn, peer):
    ws = RealishWs(conn)
    ws._request.client_address = (peer, 1)
    return ws


class SleepConn(FakeConn):
    """A send that sleeps `cost` s inside the call before returning (the
    lwip_tcp_send ERR_MEM loop: no Python deadline can cut it short)."""
    def __init__(self, clock, cost):
        FakeConn.__init__(self)
        self.clock, self.cost = clock, cost

    def send(self, view):
        self.clock.t += self.cost
        return FakeConn.send(self, view)


def hear(ws, src="sandbells-page"):
    ws.on_receive = ['{"schema":"sand.hello/v1","type":"heartbeat","src":"%s"}' % src]


def states():
    return dict((id(ws), farm_ws._meta[id(ws)]["path"].state) for ws in farm_ws._clients)


@case
def test_send_deadline_missed_cuts_that_client_through_the_model():
    farm_ws.reset_state()
    with fake_clock(step=0.001) as clock:
        stuck_conn = FakeConn(take=0)                    # TCP buffer full: not reading
        stuck = farm_ws._accept(peer_ws(stuck_conn, DISPLAY))
        good_conn = FakeConn()
        good = farm_ws._accept(peer_ws(good_conn, "192.168.0.50"))
        t0 = clock.t
        assert farm_ws.send({"seq": 1})
        took = clock.t - t0
    assert took <= link_health.SEND_TIMEOUT_S + 0.05, took     # its deadline, no more
    assert farm_ws._clients == [good] and stuck.closed
    assert stuck_conn.data == bytearray()                # cut: no close frame queued in lwIP
    assert good_conn.data.endswith(b'{"seq": 1}')        # the other client got it
    notes = dict(farm_ws.take_link_notes())
    d = notes["client_dropped"]
    assert d["how"] == "cut NEW->overran" and d["peer"] == DISPLAY, d
    assert "not reading" in d["reason"], d


@case
def test_one_send_stuck_in_lwip_cuts_that_client_and_the_lagging_one_only():
    """A send that sleeps 10 s in C: the client it was for is cut (overran),
    a LAGGING client (the likely cause) is cut (starved), a LIVE client is
    kept, and the stall is not counted towards the radio reset."""
    farm_ws.reset_state()
    with fake_clock() as clock:
        lag = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        live_conn = FakeConn()
        live = farm_ws._accept(peer_ws(live_conn, "192.168.0.50"))
        hear(lag); hear(live)
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        hear(live)
        farm_ws.poll()                                    # lag goes quiet: held
        assert states()[id(lag)] == link_health.C_LAGGING
        assert states()[id(live)] == link_health.C_LIVE
        victim = farm_ws._accept(peer_ws(SleepConn(clock, link_health.LWIP_ERR_MEM_S), "192.168.0.60"))
        farm_ws._new.remove(victim)                       # no replay: straight to a live send
        farm_ws.send({"seq": 2})
    assert victim.closed and lag.closed and farm_ws._clients == [live]
    assert live_conn.data.endswith(b'{"seq": 2}')
    hows = [i.get("how") for w, i in farm_ws.take_link_notes() if w == "client_dropped"]
    assert "cut NEW->overran" in hows and "cut LAGGING->starved" in hows, hows
    assert farm_ws.stats["starved"] == 1 and farm_ws.stats["cuts"] == 2


@case
def test_server_phase_stall_with_a_lagging_client_is_answered_not_counted():
    """14:26:31 / 14:29:43 / 14:30:10: three 10.1 s server phases ended in a
    radio reset.  With a LAGGING client to blame, each is answered by cutting
    it, no stall reaches LinkHealth, and the radio is left alone."""
    import io, contextlib
    farm_ws.reset_state()
    lh = link_health.LinkHealth()
    real = farm_ws.time.monotonic
    clock = Clock()
    farm_ws.time.monotonic = clock
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            for n in range(3):
                ingest = farm_ws._clients[0] if farm_ws._clients else farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
                old = farm_ws._accept(peer_ws(FakeConn(), "192.168.0.70"))
                hear(ingest); hear(old)
                farm_ws.poll()
                clock.t += link_health.CLIENT_LAG_S + 1
                hear(ingest)
                farm_ws.poll()                            # old: LAGGING
                farm_ws._server = StubServer(clock, link_health.LWIP_ERR_MEM_S + 0.1)
                farm_ws.poll()                            # server 10.1 s
                farm_ws._server = None
                assert old.closed and not ingest.closed, n
                _, ev = lh.update(clock.t, {"wifi_up": True, "n_clients": 1, "hb_seen": True,
                                            "last_rx_age_s": 1.0, "tower_inactive": True,
                                            "quiet_ms": 999999,
                                            "stalls": farm_ws.stalls_since(clock.t, 300.0)})
                assert "radio_reset" not in ev, (n, ev)
                clock.t += 60.0
    finally:
        farm_ws.time.monotonic = real
        farm_ws._server = None
    assert farm_ws.stalls_since(clock.t, 1000.0) == 0
    assert farm_ws.stats["stalls_answered"] == 3


@case
def test_a_stall_nobody_can_be_blamed_for_still_counts():
    import io, contextlib
    farm_ws.reset_state()
    with fake_clock() as clock:
        live = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        hear(live)
        farm_ws.poll()
        farm_ws._server = StubServer(clock, link_health.LWIP_ERR_MEM_S)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                farm_ws.poll()
        finally:
            farm_ws._server = None
        assert farm_ws._clients == [live]                # a talking client is not blamed
        assert farm_ws.stalls_since(clock.t, 300.0) == 1  # the radio rule still sees it


@case
def test_lagging_client_gets_nothing_then_everything_in_order_when_heard():
    farm_ws.reset_state()
    with fake_clock() as clock:
        conn = FakeConn()
        ws = farm_ws._accept(peer_ws(conn, DISPLAY))
        hear(ws)
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        farm_ws.poll()
        assert states()[id(ws)] == link_health.C_LAGGING
        before = len(conn.data)
        farm_ws.send({"seq": 10})
        farm_ws.send({"hb": 1}, keep=False)               # heartbeat: skipped, not queued
        farm_ws.send({"seq": 11})
        run_polls(clock, 1.0)
        assert len(conn.data) == before                   # held: nothing queued in lwIP
        hear(ws)
        run_polls(clock, 1.0)
    assert states()[id(ws)] == link_health.C_LIVE
    tail = bytes(conn.data[before:])
    assert tail.index(b'"seq": 10') < tail.index(b'"seq": 11') and b'"hb"' not in tail
    whats = [w for w, _ in farm_ws.take_link_notes()]
    assert "client_lagging" in whats and "client_resumed" in whats, whats


@case
def test_a_new_connection_replaces_the_lagging_one_from_the_same_peer():
    """The display reconnects about once a minute and leaves its old socket:
    the new connection supersedes the old one once that has stopped talking.
    A LIVE client from the same address (Sandbells ingest and the page-3
    widget share .125) is kept."""
    farm_ws.reset_state()
    with fake_clock() as clock:
        ingest = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        old_page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        hear(ingest, "sandbells"); hear(old_page)
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        hear(ingest, "sandbells")
        farm_ws.poll()                                    # old page: LAGGING
        new_page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        assert not old_page.closed                        # src not known yet
        hear(new_page)
        hear(ingest, "sandbells")
        farm_ws.poll()                                    # its hello: same peer, same src
    assert old_page.closed and farm_ws._clients == [ingest, new_page]
    d = [i for w, i in farm_ws.take_link_notes() if w == "client_dropped"][-1]
    assert d["how"] == "cut LAGGING->superseded" and "superseded" in d["reason"], d
    assert farm_ws.stats["superseded"] == 1


@case
def test_a_client_that_goes_quiet_after_its_replacement_arrived_is_cut_at_once():
    farm_ws.reset_state()
    with fake_clock() as clock:
        old_page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        hear(old_page)
        farm_ws.poll()
        new_page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))   # old still LIVE: kept
        assert not old_page.closed
        hear(new_page)
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        hear(new_page)
        farm_ws.poll()                                    # old goes quiet -> superseded
    assert old_page.closed and farm_ws._clients == [new_page]


@case
def test_the_ingest_is_never_superseded_by_the_kiosk_page_from_the_same_pi():
    """15:42:19: the ingest ('sandbells') and the kiosk page ('sandbells-page')
    both on .125, both LAGGING after a stall: the ingest was cut as
    'superseded by a newer connection'.  Different src: neither replaces
    the other."""
    farm_ws.reset_state()
    with fake_clock() as clock:
        ingest = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        hear(ingest, "sandbells"); hear(page)
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        farm_ws.poll()                                    # both quiet: LAGGING, both kept
        assert not ingest.closed and not page.closed
        page2 = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        hear(page2)
        farm_ws.poll()                                    # a new page: replaces the old page only
    assert page.closed and not ingest.closed and ingest in farm_ws._clients
    cut = [i for w, i in farm_ws.take_link_notes() if w == "client_dropped"]
    assert [c["src"] for c in cut] == ["sandbells-page"], cut


@case
def test_our_own_stall_is_not_counted_as_the_clients_quiet():
    """15:42:19: a 10 s stall in our own poll, then both clients 'no frame for
    10 s'.  The stall is credited to them: neither goes LAGGING."""
    import io, contextlib
    farm_ws.reset_state()
    with fake_clock() as clock:
        ingest = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        hear(ingest, "sandbells"); hear(page)
        farm_ws.poll()
        clock.t += 1.0
        class Srv:
            def poll(self):
                clock.t += link_health.LWIP_ERR_MEM_S         # our 10 s
        farm_ws._server = Srv()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                farm_ws.poll()
        finally:
            farm_ws._server = None
        assert states() == {id(ingest): link_health.C_LIVE, id(page): link_health.C_LIVE}, states()
        clock.t += link_health.DRAIN_S + 0.1                 # the drain ends; still nothing heard
        farm_ws.poll()
        notes = farm_ws.take_link_notes()
        assert not [w for w, _ in notes if w == "client_lagging"], (notes, states())
        clock.t += link_health.CLIENT_LAG_S + 1                # and now really quiet
        farm_ws.poll()
    assert states() == {id(ingest): link_health.C_LAGGING, id(page): link_health.C_LAGGING}, states()


@case
def test_broadcast_to_every_stuck_client_stays_inside_the_poll_budget():
    farm_ws.reset_state()
    with fake_clock(step=0.001) as clock:
        for n in range(farm_ws.MAX_WS_CLIENTS):
            farm_ws._accept(peer_ws(FakeConn(take=0), "192.168.0.%d" % (60 + n)))
        t0 = clock.t
        farm_ws.send({"seq": 1})
        took = clock.t - t0
    assert took <= link_health.POLL_BUDGET_S + link_health.SEND_TIMEOUT_S, took
    for ws in list(farm_ws._clients):                     # whoever was not reached waits
        assert farm_ws._meta[id(ws)]["todo"], "left over: queued for the next poll"


@case
def test_status_shows_clients_by_state():
    farm_ws.reset_state()
    with fake_clock() as clock:
        a = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        farm_ws._accept(peer_ws(FakeConn(), "192.168.0.50"))
        hear(a)
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        farm_ws.poll()
    st = farm_ws.link_status()
    assert st["clients_by_state"] == {"LAGGING": 1, "NEW": 1}, st
    assert st["lagging"] == 1



# --- the 2026-10-29 14:45 failure: server phase 10.1 s with 0 clients ---------

class StarvedPool:
    """lwIP's shared buffers.  Starved until `free_at`: any reply the library
    sends meanwhile sleeps LWIP_ERR_MEM_S inside send().  Closed connections
    are aborted (and their segments freed) CLOSE_ABORT_S after the stall."""
    def __init__(self, clock, starved_until):
        self.clock, self.free_at = clock, starved_until
        self.server_calls = []
        self.stalls = []

    def poll(self):                                   # Server.poll: a request comes in, a reply goes out
        self.server_calls.append(self.clock.t)
        if self.clock.t < self.free_at:
            self.stalls.append(self.clock.t)
            self.clock.t += link_health.LWIP_ERR_MEM_S
            self.free_at = min(self.free_at, self.clock.t + link_health.CLOSE_ABORT_S)


@case
def test_zero_client_server_stall_drains_once_instead_of_repeating():
    """Every poll the display (or a /status poller) has a request waiting.
    The first reply hits ERR_MEM (10 s, unavoidable).  Then SEND_DRAIN: no
    Server.poll() at all for DRAIN_S, so not one more reply goes into the
    starved pool; then the server is served again and the pool is free.
    One stall in five minutes, nothing for LinkHealth to reset."""
    import io, contextlib
    farm_ws.reset_state()
    real = farm_ws.time.monotonic
    clock = Clock()
    farm_ws.time.monotonic = clock
    pool = StarvedPool(clock, starved_until=clock.t + 30.0)   # would stay starved 30 s if fed
    farm_ws._server = pool
    lh = link_health.LinkHealth()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            t_end = clock.t + 300.0
            while clock.t < t_end:
                farm_ws.poll()
                _, ev = lh.update(clock.t, {"wifi_up": True, "n_clients": 0, "hb_seen": True,
                                            "last_rx_age_s": 1.0, "tower_inactive": True,
                                            "quiet_ms": 999999,
                                            "stalls": farm_ws.stalls_since(clock.t, 300.0)})
                assert "radio_reset" not in ev, ev
                clock.t += 0.05
    finally:
        farm_ws.time.monotonic = real
        farm_ws._server = None
    assert len(pool.stalls) == 1, pool.stalls                 # once, not 14:26 / 14:29 / 14:30
    first = pool.stalls[0] + link_health.LWIP_ERR_MEM_S
    assert not [t for t in pool.server_calls if first <= t < first + link_health.DRAIN_S - 0.05]
    assert [t for t in pool.server_calls if t >= first + link_health.DRAIN_S]   # served again
    assert farm_ws.link_status()["send_state"] in ("SEND_DEGRADED", "SEND_OK")


@case
def test_nothing_is_written_while_lwip_drains_and_it_all_goes_out_after():
    farm_ws.reset_state()
    with fake_clock() as clock:
        conn = FakeConn()
        ws = farm_ws._accept(peer_ws(conn, DISPLAY))
        farm_ws._new.remove(ws)
        hear(ws)
        farm_ws.poll()
        farm_ws._server = StubServer(clock, link_health.LWIP_ERR_MEM_S)
        try:
            farm_ws.poll()                                # starved: SEND_DRAIN
        finally:
            farm_ws._server = None
        assert farm_ws._path.state == link_health.SEND_DRAIN
        before = len(conn.data)
        farm_ws.send({"seq": 1})
        farm_ws.send({"hb": 1}, keep=False)
        ws.on_receive = ['{"type":"heartbeat"}'] * 2000   # it keeps talking throughout
        run_polls(clock, link_health.DRAIN_S - 1.0)
        assert len(conn.data) == before                   # held for everyone
        assert ws in farm_ws._clients                     # LIVE: not blamed
        run_polls(clock, 2.0)
    assert farm_ws._path.state != link_health.SEND_DRAIN
    assert conn.data[before:].endswith(b'{"seq": 1}') and b'"hb"' not in conn.data[before:]


# --- 2026-10-29 15:06/15:07: the /status reply, 5964 B in one send() -------

class ReplyConn:
    """An accepted /status socket.  `cost(n)` -> seconds one send() of n bytes
    sleeps inside lwIP; `eagain` makes every send raise EAGAIN."""
    def __init__(self, clock, cost=None, eagain=False):
        self.clock, self.cost, self.eagain = clock, cost, eagain
        self.sends = []                 # (clock time, bytes asked)
        self.data = bytearray()
        self.closed = False
        self.blocking = True

    def setblocking(self, flag):
        self.blocking = flag

    def send(self, view):
        self.sends.append((self.clock.t, len(view)))
        if self.eagain:
            raise OSError(farm_ws.EAGAIN, "EAGAIN")
        if self.cost:
            self.clock.t += self.cost(len(view))
        self.data.extend(bytes(view))
        return len(view)

    def close(self):
        self.closed = True


STATUS_B = 5964                         # the logged reply size


def lh_obs(clock, n_clients=0):
    return {"wifi_up": True, "n_clients": n_clients, "hb_seen": True, "last_rx_age_s": 1.0,
            "tower_inactive": True, "quiet_ms": 999999,
            "stalls": farm_ws.stalls_since(clock.t, 300.0)}


@case
def test_status_reply_goes_out_one_mss_chunk_per_turn_and_never_blocks_the_poll():
    farm_ws.reset_state()
    with fake_clock() as clock:
        conn = ReplyConn(clock)
        farm_ws._queue_reply(conn, b"x" * STATUS_B, "192.168.0.125")
        assert not conn.blocking                          # EAGAIN, not a wait
        turns = 0
        while not conn.closed and turns < 50:
            n_before = len(conn.sends)
            farm_ws.poll()
            assert len(conn.sends) - n_before <= link_health.REPLY_CHUNKS_PER_TURN
            turns += 1
            clock.t += 0.05
    assert bytes(conn.data) == b"x" * STATUS_B
    assert max(n for _, n in conn.sends) <= link_health.REPLY_CHUNK_B == link_health.TCP_MSS
    assert turns == -(-STATUS_B // link_health.REPLY_CHUNK_B) == 5, turns
    assert farm_ws.stats["replies_done"] == 1 and farm_ws.stats["last_reply_b"] == STATUS_B


@case
def test_reply_under_a_starved_lwip_is_cancelled_drained_and_answered_not_reset():
    """15:06:41 and 15:07:17 again, with the writer: a chunk sleeps 10 s in
    lwIP.  The reply is cancelled, the send path drains, the stall is the
    reply's (answered), and two of them do not reach the radio reset."""
    import io, contextlib
    farm_ws.reset_state()
    lh = link_health.LinkHealth()
    real = farm_ws.time.monotonic
    clock = Clock()
    farm_ws.time.monotonic = clock
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            for n in range(3):
                conn = ReplyConn(clock, cost=lambda b: link_health.LWIP_ERR_MEM_S)
                farm_ws._queue_reply(conn, b"x" * STATUS_B, "192.168.0.125")
                farm_ws.poll()
                assert conn.closed and len(conn.sends) == 1, n     # one chunk, then cancelled
                assert farm_ws._path.state == link_health.SEND_DRAIN
                _, ev = lh.update(clock.t, lh_obs(clock))
                assert "radio_reset" not in ev, (n, ev)
                clock.t += 36.0                                    # 15:06:41 -> 15:07:17
                farm_ws.poll()
    finally:
        farm_ws.time.monotonic = real
    assert farm_ws.stats["replies_cancelled"] == 3 and farm_ws.stats["stalls_answered"] == 3
    assert farm_ws.stalls_since(clock.t, 300.0) == 0
    notes = [i for w, i in farm_ws.take_link_notes() if w == "reply_cancelled"]
    assert notes and "took 10.0 s" in notes[0]["why"] and notes[0]["size_b"] == STATUS_B, notes


@case
def test_reply_that_lwip_never_takes_is_cancelled_at_its_deadline():
    farm_ws.reset_state()
    with fake_clock(step=0.001) as clock:
        conn = ReplyConn(clock, eagain=True)
        farm_ws._queue_reply(conn, b"x" * STATUS_B)
        t0, worst = clock.t, 0.0
        while not conn.closed and clock.t - t0 < 10.0:
            a = clock.t
            farm_ws.poll()
            worst = max(worst, clock.t - a)
            clock.t += 0.05
    assert conn.closed and farm_ws.stats["replies_cancelled"] == 1
    assert link_health.REPLY_DEADLINE_S <= clock.t - t0 <= link_health.REPLY_DEADLINE_S + 0.2
    assert worst < 0.1, worst                         # EAGAIN yields: the poll is never held
    notes = dict(farm_ws.take_link_notes())
    assert "deadline" in notes["reply_cancelled"]["why"], notes


@case
def test_one_reply_is_written_at_a_time_and_a_third_is_turned_away():
    farm_ws.reset_state()
    with fake_clock() as clock:
        a, b, c = ReplyConn(clock), ReplyConn(clock), ReplyConn(clock)
        farm_ws._queue_reply(a, b"a" * 3000)
        farm_ws._queue_reply(b, b"b" * 3000)
        farm_ws._queue_reply(c, b"c" * 3000)              # REPLY_QUEUE_MAX = 2: closed at once
        assert c.closed and not c.sends
        farm_ws.poll()
        assert a.sends and not b.sends                    # the single writer
        while not a.closed:
            clock.t += 0.05
            farm_ws.poll()
            assert not b.sends or a.closed
        while not b.closed:
            clock.t += 0.05
            farm_ws.poll()
    assert bytes(a.data) == b"a" * 3000 and bytes(b.data) == b"b" * 3000
    assert farm_ws.stats["replies_done"] == 2 and farm_ws.stats["replies_cancelled"] == 1


@case
def test_nothing_of_a_reply_is_written_while_the_send_path_drains():
    farm_ws.reset_state()
    with fake_clock() as clock:
        farm_ws._path.observe(clock.t, "server", link_health.LWIP_ERR_MEM_S, True)
        assert farm_ws._path.state == link_health.SEND_DRAIN
        conn = ReplyConn(clock)
        farm_ws._queue_reply(conn, b"x" * 1000)
        run_polls(clock, 1.0)
    assert not conn.sends                             # held, and its deadline still runs


@case
def test_queued_response_collects_the_librarys_headers_and_body():
    class LibResponse:                                # the part of 4.7.1 Response it uses
        def __init__(self, request, body="", content_type=None, headers=None):
            self._request, self._body, self._content_type = request, body, content_type
        def _send_headers(self, content_length=None, content_type=None):
            self._send_bytes(self._request.connection,
                             ("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % content_length).encode())
        def _send_bytes(self, conn, buf):
            raise AssertionError("must not write")
    farm_ws.reset_state()
    saved = (farm_ws.Response, list(farm_ws._queued_cls))
    farm_ws.Response = LibResponse
    del farm_ws._queued_cls[:]
    try:
        with fake_clock() as clock:
            conn = ReplyConn(clock)
            req = types.SimpleNamespace(connection=conn, client_address=("192.168.0.125", 5))
            resp = farm_ws._queued_response_class()(req, body='{"a": 1}', content_type="application/json")
            resp._send()                              # what Server.poll() calls
            assert not conn.sends and not conn.closed and len(farm_ws._replies) == 1
            run_polls(clock, 0.5)
    finally:
        farm_ws.Response = saved[0]
        farm_ws._queued_cls[:] = saved[1]
    assert bytes(conn.data) == b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n{"a": 1}' and conn.closed


@case
def test_a_server_phase_stall_in_the_librarys_own_reply_is_answered():
    import io, contextlib
    import ws_bounds
    farm_ws.reset_state()
    with fake_clock() as clock:
        class Srv:
            def poll(self):
                clock.t += link_health.LWIP_ERR_MEM_S
                ws_bounds.last["reply_s"] = link_health.LWIP_ERR_MEM_S    # a handshake / 503
        farm_ws._server = Srv()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                farm_ws.poll()
        finally:
            farm_ws._server = None
    assert farm_ws.stalls_since(clock.t, 300.0) == 0 and farm_ws.stats["stalls_answered"] == 1


@case
def test_a_close_frame_that_fails_at_once_does_not_raise_the_backoff():
    """15:04:30: 'close NEW->failed' EBADF, then send_backoff 'close failed 0.00 s'."""
    farm_ws.reset_state()
    class DeadConn(FakeConn):
        def send(self, view):
            raise OSError(9, "EBADF")
        def close(self):
            pass
    ws = farm_ws._accept(peer_ws(DeadConn(), DISPLAY))
    farm_ws._event(ws, "failed", OSError(9, "EBADF"))
    assert farm_ws._path.state == link_health.SEND_OK, farm_ws._path.state
    assert not [w for w, _ in farm_ws.take_link_notes() if w == "send_backoff"]


@case
def test_hello_src_says_which_client_it_was():
    farm_ws.reset_state()
    with fake_clock() as clock:
        page = farm_ws._accept(peer_ws(FakeConn(), DISPLAY))
        page.on_receive = ['{"schema":"sand.hello/v1","type":"heartbeat","src":"sandbells-page"}']
        farm_ws.poll()
        clock.t += link_health.CLIENT_LAG_S + 1
        farm_ws.poll()
    lag = [i for w, i in farm_ws.take_link_notes() if w == "client_lagging"][0]
    assert lag["src"] == "sandbells-page", lag


# --- tower ringing: /status held so strokes have the link ------------------

class _HdrResponse:                        # the part of the library Response used
    def __init__(self, request, body="", content_type=None, headers=None):
        self._request, self._body, self._content_type = request, body, content_type
        self._headers = dict(headers or {})
    def _send_headers(self, content_length=None, content_type=None):
        h = "".join("%s: %s\r\n" % kv for kv in sorted(self._headers.items()))
        self._send_bytes(self._request.connection,
                         ("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%s\r\n"
                          % (content_length, h)).encode())
    def _send_bytes(self, conn, buf):
        raise AssertionError("must not write")


def _payload(calls, n_sections=6, pad=900, extra=None):
    """A document the shape of the concentrator's (about 7 kB with parts)."""
    def fn():
        calls.append(1)
        doc = {"schema": "sand.status/v1", "id": "sandswing", "ts": len(calls),
               "summary": "last line", "panel": [[0, 0, 0]] * 6,
               "sections": [{"id": "s%d" % i, "title": "S%d" % i,
                             "items": [{"key": "k", "label": "K", "value": "x" * pad}]}
                            for i in range(n_sections)],
               "parts": {"link": {"pad": "p" * 1500}, "ring": {"n": 1}}}
        if extra:
            extra(doc)
        return doc
    return fn


def _status_get(clock, calls, query=None, peer="192.168.0.125", payload=None):
    """One GET /status through route_status and the reply writer -> (bytes, turns)."""
    saved = (farm_ws.Response, list(farm_ws._queued_cls), farm_ws._payload_fn)
    farm_ws.Response = _HdrResponse
    del farm_ws._queued_cls[:]
    farm_ws._payload_fn = payload or _payload(calls)
    try:
        conn = ReplyConn(clock)
        req = types.SimpleNamespace(connection=conn, client_address=(peer, 5),
                                    query_params=dict(query or {}))
        farm_ws.route_status(req)._send()
        turns = 0
        while not conn.closed and turns < 40:
            farm_ws.poll()
            clock.t += 0.05
            turns += 1
    finally:
        farm_ws.Response, farm_ws._payload_fn = saved[0], saved[2]
        farm_ws._queued_cls[:] = saved[1]
    return bytes(conn.data), turns


def _body(raw):
    head, body = raw.split(b"\r\n\r\n", 1)
    return head, json.loads(body)


@case
def test_while_the_tower_rings_status_is_a_tiny_held_document_with_retry_after():
    farm_ws.reset_state()
    calls = []
    with fake_clock() as clock:
        full, turns_full = _status_get(clock, calls)
        assert farm_ws.set_tower("active") == "hold"
        clock.t += 20
        held, turns_held = _status_get(clock, calls)
        assert farm_ws.set_tower("ringing_down") is None          # still ringing: still held
        clock.t += 20
        held2, _ = _status_get(clock, calls)
        assert farm_ws.set_tower("inactive") == "resume"
        clock.t += 20
        back, _ = _status_get(clock, calls)
    assert len(calls) == 2                                    # no payload built while held
    assert turns_full >= 5 and turns_held == 1
    head, doc = _body(held)
    assert b"Retry-After: %d" % link_health.STATUS_HELD_RETRY_S in head
    assert len(held) <= link_health.STATUS_HELD_BODY_MAX <= link_health.REPLY_CHUNK_B, len(held)
    assert doc["schema"] == "sand.status/v1" and doc["held"] == "ringing" and doc["tower"] == "active"
    assert doc["sections"][0]["items"][0]["value"] == "active"
    assert _body(held2)[1]["tower"] == "ringing_down"
    assert "parts" in _body(back)[1] and b"Retry-After" not in back   # full page again
    notes = [w for w, _ in farm_ws.take_link_notes()]
    assert "status_held" in notes and "status_resumed" in notes
    assert farm_ws.stats["replies_held"] == 2


@case
def test_status_since_gives_only_what_changed_and_a_full_document_first():
    farm_ws.reset_state()
    calls = []
    def tower_changes(doc):
        if len(calls) >= 2:
            doc["sections"][3]["items"][0]["value"] = "changed"
    pay = _payload(calls, extra=tower_changes)
    with fake_clock() as clock:
        raw, _ = _status_get(clock, calls, {"since": "0", "boot": ""}, payload=pay)
        _, first = _body(raw)
        assert first.get("full") is True, first and "parts" not in first     # lean: Sandbells never shows parts
        v, boot = first["status_version"], first["boot_id"]
        assert len(first["sections"]) == 6
        clock.t += link_health.STATUS_REBUILD_S + 0.5
        raw, turns = _status_get(clock, calls, {"since": str(v), "boot": boot}, payload=pay)
        _, d = _body(raw)
        assert d.get("delta") is True and d["since"] == v and d["status_version"] == v + 1, d
        assert [s["id"] for s in d["sections"]] == ["s3"], d          # only the changed one
        assert d["order"] == ["s0", "s1", "s2", "s3", "s4", "s5"] and "head" in d, d
        assert len(raw) < 1500 and turns == 1, (len(raw), turns)
        clock.t += 1.0                                     # within STATUS_REBUILD_S: no rebuild
        raw, _ = _status_get(clock, calls, {"since": str(v + 1), "boot": boot}, payload=pay,
                             peer="192.168.0.120")          # (.125 has had its two)
        _, d = _body(raw)
        assert d["sections"] == [] and "head" not in d and len(calls) == 2, (d, len(calls))
        clock.t += 5
        raw, _ = _status_get(clock, calls, {"since": str(v), "boot": "deadbeef"}, payload=pay)
        assert _body(raw)[1].get("full") is True, _body(raw)   # another boot: whole document
    assert farm_ws.stats["deltas"] == 2 and farm_ws.stats["fulls"] == 2, farm_ws.stats


@case
def test_status_document_is_never_one_big_allocation():
    """15:42-15:47: 'allocating 6728 bytes' (json.dumps of the whole doc) and
    'allocating 7382 bytes' (headers + body joined).  The reply is pieces,
    each one unit of the document, and is written through one preallocated
    REPLY_CHUNK_B buffer."""
    farm_ws.reset_state()
    calls = []
    with fake_clock() as clock:
        saved = (farm_ws.Response, list(farm_ws._queued_cls), farm_ws._payload_fn)
        farm_ws.Response = _HdrResponse
        del farm_ws._queued_cls[:]
        farm_ws._payload_fn = _payload(calls)
        try:
            conn = ReplyConn(clock)
            req = types.SimpleNamespace(connection=conn, client_address=("192.168.0.125", 5),
                                        query_params={})
            farm_ws.route_status(req)._send()
            job = farm_ws._replies[0]
            assert job["size"] > 7000, job["size"]
            assert max(len(p) for p in job["pieces"]) <= 2000, [len(p) for p in job["pieces"]]
            buf = farm_ws._chunk_buf
            while not conn.closed:
                farm_ws.poll()
                clock.t += 0.05
            assert farm_ws._chunk_buf is buf
        finally:
            farm_ws.Response, farm_ws._payload_fn = saved[0], saved[2]
            farm_ws._queued_cls[:] = saved[1]
    doc = json.loads(bytes(conn.data).split(b"\r\n\r\n", 1)[1])
    assert len(doc["sections"]) == 6 and doc["parts"]["ring"] == {"n": 1}
    assert max(n for _, n in conn.sends) <= link_health.REPLY_CHUNK_B


@case
def test_status_is_rate_limited_per_peer_and_a_third_poller_is_held():
    farm_ws.reset_state()
    calls = []
    with fake_clock() as clock:
        q = {"since": "0", "boot": ""}
        a1, _ = _status_get(clock, calls, q)
        a2, _ = _status_get(clock, calls, q)                 # two per STATUS_POLL_HINT_S: fine
        a3, _ = _status_get(clock, calls, q)                 # a third within it: held "again"
        b1, _ = _status_get(clock, calls, q, peer="192.168.0.120")
        c1, _ = _status_get(clock, calls, q, peer="192.168.0.99")   # a third poller: "crowd"
        clock.t += link_health.STATUS_POLL_HINT_S
        a4, _ = _status_get(clock, calls, q)
        p1, _ = _status_get(clock, calls, None, peer="192.168.0.120")   # plain: whole + parts
        clock.t += link_health.STATUS_POLL_HINT_S
        p2, _ = _status_get(clock, calls, None, peer="192.168.0.120")   # plain again < 15 s
    got = [_body(x)[1].get("held") for x in (a1, a2, a3, b1, c1, a4, p1, p2)]
    assert got == [None, None, "again", None, "crowd", None, None, "refull"], got
    assert "parts" in _body(p1)[1]


@case
def test_a_memory_error_is_an_event_not_the_end_of_the_link():
    """15:42:29 / 15:45-47: 'ws poll: memory allocation failed' every few
    seconds, then lost.  Now: replies cancelled, document dropped, heap
    collected, the send path drains without raising the level, /status is
    held for STATUS_NOMEM_S, and the stall is answered."""
    import io, contextlib
    farm_ws.reset_state()
    calls = []
    with fake_clock() as clock:
        _status_get(clock, calls)
        conn = ReplyConn(clock)
        farm_ws._queue_reply(conn, [b"x" * 3000])
        class Srv:
            def poll(self):
                raise MemoryError("memory allocation failed, allocating 7382 bytes")
        farm_ws._server = Srv()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                farm_ws.poll()
        finally:
            farm_ws._server = None
        assert conn.closed and not farm_ws._replies and farm_ws._doc.built_at is None
        assert farm_ws._path.state == link_health.SEND_DRAIN and farm_ws._path.level == 0
        clock.t += link_health.DRAIN_S + 0.5              # nothing is served during the drain
        raw, _ = _status_get(clock, calls, {"since": "0", "boot": ""})
        assert _body(raw)[1]["held"] == "nomem"
        clock.t += link_health.STATUS_NOMEM_S
        raw, _ = _status_get(clock, calls, {"since": "0", "boot": ""})
        assert _body(raw)[1]["full"] is True
    notes = dict(farm_ws.take_link_notes())
    assert "mem_low" in notes and notes["mem_low"]["where"] == "server", notes
    assert farm_ws.stats["mem_low"] == 1 and farm_ws.stalls_since(clock.t, 300.0) == 0


@case
def test_a_memory_error_building_the_document_is_held_not_raised():
    import io, contextlib
    farm_ws.reset_state()
    calls = []
    def boom():
        raise MemoryError("memory allocation failed, allocating 6728 bytes")
    saved = (farm_ws.Response, list(farm_ws._queued_cls), farm_ws._payload_fn)
    farm_ws.Response = _HdrResponse
    del farm_ws._queued_cls[:]
    farm_ws._payload_fn = boom
    try:
        with fake_clock() as clock, contextlib.redirect_stdout(io.StringIO()):
            req = types.SimpleNamespace(connection=ReplyConn(clock),
                                        client_address=("192.168.0.125", 5), query_params={})
            farm_ws.route_status(req)._send()      # nothing raised into the library
    finally:
        farm_ws.Response, farm_ws._payload_fn = saved[0], saved[2]
        farm_ws._queued_cls[:] = saved[1]
    body = status_doc.join(farm_ws._replies[0]["pieces"]).split(b"\r\n\r\n", 1)[1]
    assert json.loads(body)["held"] == "nomem" and farm_ws.stats["mem_low"] == 1
    assert farm_ws._path.state == link_health.SEND_DRAIN


@case
def test_a_slow_status_chunk_does_not_raise_the_ws_backoff():
    """15:38:33 send_backoff 'reply slow 0.95 s'."""
    farm_ws.reset_state()
    with fake_clock() as clock:
        conn = ReplyConn(clock, cost=lambda b: 0.95)
        farm_ws._queue_reply(conn, [b"x" * 3000])
        for _ in range(5):
            farm_ws.poll()
            clock.t += 0.05
    assert farm_ws._path.state == link_health.SEND_OK and farm_ws._path.level == 0


@case
def test_ringing_cancels_the_full_reply_being_written():
    farm_ws.reset_state()
    with fake_clock() as clock:
        conn = ReplyConn(clock)
        farm_ws._queue_reply(conn, b"x" * STATUS_B)
        farm_ws.poll()                                      # one chunk out
        assert len(conn.sends) == 1 and not conn.closed
        farm_ws.set_tower("active")
        assert conn.closed and not farm_ws._replies
        clock.t += 0.05
        farm_ws.poll()
    assert len(conn.sends) == 1                             # nothing more written
    held = [i for w, i in farm_ws.take_link_notes() if w == "status_held"]
    assert held and held[0]["cancelled"] == 1, held


@case
def test_a_status_socket_already_gone_is_dropped_quietly():
    """16:20:03-04: three '/status reply cancelled ([Errno 9] EBADF)' warnings."""
    import io, contextlib
    farm_ws.reset_state()
    class GoneConn(ReplyConn):
        def send(self, view):
            if self.sends:
                raise OSError(9, "EBADF")
            return ReplyConn.send(self, view)
    with fake_clock() as clock:
        conn = GoneConn(clock)
        farm_ws._queue_reply(conn, [b"x" * 1773])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(3):
                farm_ws.poll()
                clock.t += 0.05
    assert conn.closed and not farm_ws._replies
    assert farm_ws.stats["replies_gone"] == 1 and farm_ws.stats["replies_cancelled"] == 0
    assert "cancelled" not in out.getvalue()
    assert not [w for w, _ in farm_ws.take_link_notes() if w == "reply_cancelled"]
    assert farm_ws._path.state == link_health.SEND_OK


@case
def test_parts_are_built_only_for_a_plain_status():
    farm_ws.reset_state()
    asked = []
    def payload(parts=True):
        asked.append(parts)
        doc = {"schema": "sand.status/v1", "id": "x", "sections": [{"id": "a"}],
               "parts": {"watchdog": {"last_reset": None}}}
        if parts:
            doc["parts"]["survey"] = {"pad": "s" * 3000}
        return doc
    with fake_clock() as clock:
        raw, _ = _status_get(clock, [], {"since": "0", "boot": ""}, payload=payload)
        assert "parts" not in _body(raw)[1] and asked == [False]
        lean_cache = farm_ws.stats["status_cache_b"]
        clock.t += 0.5                                       # within STATUS_REBUILD_S
        raw, _ = _status_get(clock, [], None, peer="192.168.0.120", payload=payload)
        assert _body(raw)[1]["parts"]["survey"]["pad"] == "s" * 3000 and asked == [False, True]
        clock.t += 5
        _status_get(clock, [], {"since": "0", "boot": ""}, payload=payload)
    assert asked == [False, True, False]
    assert farm_ws.stats["status_cache_b"] == lean_cache < 500, farm_ws.stats["status_cache_b"]


def kiosk_session(seconds, drain_bps, cap=3000):
    """A display that reconnects half a second after every drop, against an lwIP
    queue model.  Returns (stall times, 503 count, replays done, connects)."""
    farm_ws.reset_state()
    LwipConn.stalls = []
    refusals = []
    real = (farm_ws.time.monotonic, farm_ws._busy_response, farm_ws.Websocket)
    clock = Clock()
    farm_ws.time.monotonic = clock
    farm_ws._busy_response = lambda request, retry: refusals.append(retry) or "503"
    farm_ws.Websocket = lambda request: RealishWs(LwipConn(clock, cap, drain_bps))
    try:
        for n in range(farm_ws.REPLAY_N):
            farm_ws.send(big(n))
        t0 = clock.t
        t_end = t0 + seconds
        while clock.t < t_end:
            if not farm_ws._clients:
                farm_ws.route_ws(types.SimpleNamespace())
            farm_ws.poll()
            busy = any(m["todo"] for m in farm_ws._meta.values())
            clock.t += 0.05 if busy else 0.5
        stalls = [round(t - t0, 1) for t in LwipConn.stalls]
        return stalls, len(refusals), farm_ws.stats["replays_done"], farm_ws.stats["connects"]
    finally:
        farm_ws.time.monotonic, farm_ws._busy_response, farm_ws.Websocket = real


@case
def test_display_reconnect_storm_stops_after_the_ladder():
    """The log: connect, replay blocks 10 s, dropped, reconnect at once, again.
    A slow link (1 kB/s) may stall twice while the model climbs; after that the
    reconnects are refused cheaply, then served with a replay that fits, and
    there is no third stall for the next 100 minutes."""
    stalls, refused, replays, connects = kiosk_session(6000.0, 1000.0)
    assert 1 <= len(stalls) <= 2, stalls            # the ladder, not a storm
    assert stalls[-1] < 120.0, stalls               # settled within two minutes
    assert refused >= 1                             # the instant reconnects got a 503
    assert replays >= 1                             # and the display was served in the end
    assert farm_ws._path.level in (2, 3), farm_ws._path.level
    assert farm_ws.stats["refused_backoff"] == refused


@case
def test_a_healthy_link_is_never_slowed_or_refused():
    stalls, refused, replays, connects = kiosk_session(300.0, 200000.0)
    assert stalls == [] and refused == 0 and connects == 1 and replays == 1
    assert farm_ws._path.state == "SEND_OK" and farm_ws._path.level == 0


@case
def test_busy_response_is_a_503_with_retry_after():
    sent = []
    class R:
        def __init__(self, request, body="", status=None, headers=None):
            sent.append((body, status, headers))
    saved = getattr(farm_ws, "Response", None)
    farm_ws.Response = R
    try:
        farm_ws._busy_response(object(), 2.0)
    finally:
        farm_ws.Response = saved
    body, status, headers = sent[0]
    assert status[0] == 503 and headers["Retry-After"] == "2", sent


@case
def test_live_events_wait_behind_a_replay_in_progress_in_order():
    fresh(0)
    for n in range(6):
        farm_ws.send({"seq": n})
    a = farm_ws._accept(FakeWs())
    with fake_clock() as clock:
        farm_ws.poll()                               # first chunk only
        assert len(a.sent) <= 1
        farm_ws.send({"seq": 6})                     # a stroke while replaying
        farm_ws.send({"hb": 1}, keep=False)          # heartbeats are skipped meanwhile
        run_polls(clock, 5.0)
    assert [json.loads(m)["seq"] for m in a.sent] == [0, 1, 2, 3, 4, 5, 6], a.sent
    assert not any("hb" in m for m in a.sent)


@case
def test_notes_for_the_log_say_what_the_model_decided():
    farm_ws.reset_state()
    with fake_clock(step=0.001) as clock:
        farm_ws._accept(RealishWs(FakeConn(take=0)))     # a peer whose buffer never drains
        farm_ws.send({"seq": 1})                         # live send times out: dropped, bad
        farm_ws.poll()
        whats = [w for w, _ in farm_ws.take_link_notes()]
        assert "send_backoff" in whats, whats
        clock.t += 3.0
        farm_ws.poll()
        notes = dict(farm_ws.take_link_notes())
        assert "send_degraded" in notes and notes["send_degraded"]["level"] == 1, notes
    assert farm_ws.link_status()["send_state"] == "SEND_DEGRADED"


finish()
