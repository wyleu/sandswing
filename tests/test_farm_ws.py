# tests for farm_ws.py (several /ws clients; fake network modules)
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
        return bytearray([0x80 | opcode, len(message)]) + message


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


@case
def test_new_client_gets_recent_events_replayed_not_heartbeats():
    fresh(0)
    for n in range(farm_ws.REPLAY_N + 5):
        farm_ws.send({"seq": n + 1})              # nobody connected: kept
    farm_ws.send({"heartbeat": 1}, keep=False)
    a = farm_ws._accept(FakeWs())
    assert not a.sent                            # handshake first
    farm_ws.poll()
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
            t[0] += 5.0                         # the library holds the loop 5 s
    saved = (farm_ws.time.monotonic, farm_ws._server, farm_ws._send_text, farm_ws._receive)
    farm_ws.time.monotonic = lambda: t[0]
    farm_ws._server = Srv()
    farm_ws._send_text = lambda ws, msg: t.__setitem__(0, t[0] + 1.0)      # replay 1 s
    farm_ws._receive = lambda ws, now: t.__setitem__(0, t[0] + 34.0)       # reads 34 s
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            farm_ws.poll()
    finally:
        farm_ws.time.monotonic, farm_ws._server, farm_ws._send_text, farm_ws._receive = saved
    out = buf.getvalue()
    assert "ws poll took 40.0 s: server 5.0, replay 1.0, read 34.0" in out, out


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
    clock = [0.0]
    real = farm_ws.time.monotonic
    def tick():
        clock[0] += 0.7                      # every look at the clock costs 0.7 s
        return clock[0]
    sent = []
    a_send = farm_ws._send_text
    def slow_send(ws, msg):
        sent.append(msg)
    farm_ws._send_text = slow_send
    farm_ws.time.monotonic = tick
    try:
        farm_ws.poll()
    finally:
        farm_ws.time.monotonic = real
        farm_ws._send_text = a_send
    assert len(sent) < 10, len(sent)         # gave up part way
    assert a not in farm_ws._clients         # and dropped the client


finish()
