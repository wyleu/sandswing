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
