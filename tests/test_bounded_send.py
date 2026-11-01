# test_bounded_send.py -- no network operation holds the loop longer than
# link.send_block_ms (farm_ws._flush, _close, the handshake), shown with
# tests/fake_socket.py: a socket that blocks, says EAGAIN, takes a few bytes
# at a time, or was shut under us (EBADF).  2026-10-31 10:41-10:53: a laggy
# client (sandbells2, 192.168.0.22) and the 10 s it cost each time.
import errno
import sys
import types

from support import case, finish
from fake_socket import FakeSocket

for name in ("wifi", "adafruit_connection_manager"):
    sys.modules.setdefault(name, types.ModuleType(name))
_http = types.ModuleType("adafruit_httpserver")


class Ws:
    """The adafruit_httpserver Websocket surface farm_ws uses."""
    def __init__(self, request):
        self._request = request
        self.closed = False

    @staticmethod
    def _prepare_frame(opcode, message):
        n = len(message)
        if n < 126:
            return bytearray([0x80 | opcode, n]) + message
        return bytearray([0x80 | opcode, 126]) + n.to_bytes(2, "big") + message

    def receive(self, fail_silently=False):
        return None


for attr in ("Server", "Request", "Response", "GET"):
    setattr(_http, attr, object)
_http.Websocket = Ws
sys.modules["adafruit_httpserver"] = _http

import farm_ws
import link_health
import ws_bounds


class Clock:
    """Fake monotonic; every look costs `step` (a busy loop burns time)."""
    def __init__(self, step=0.0005):
        self.t = 1000.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class rig:
    def __enter__(self):
        farm_ws.reset_state()
        self.clock = Clock()
        self.real = (farm_ws.time.monotonic, ws_bounds._now)
        farm_ws.time.monotonic = self.clock
        ws_bounds._now = self.clock
        return self

    def __exit__(self, *a):
        farm_ws.time.monotonic, ws_bounds._now = self.real
        farm_ws._server = None
        farm_ws.configure_bounds({})

    def client(self, script, peer="192.168.0.22", replay=False):
        sock = FakeSocket(self.clock, script, (peer, 50000))
        ws = farm_ws._accept(Ws(types.SimpleNamespace(connection=sock, client_address=sock.peer)))
        if not replay:
            farm_ws._new.remove(ws)
        return ws, sock

    def timed(self, fn, *a):
        t0 = self.clock.t
        fn(*a)
        return self.clock.t - t0

    def polls(self, seconds, step=0.05):
        worst, end = 0.0, self.clock.t + seconds
        while self.clock.t < end:
            worst = max(worst, self.timed(farm_ws.poll))
            self.clock.t += step
        return worst


BOUND = link_health.SEND_TIMEOUT_S
LOOP_SLICE = 0.05          # what one send()/poll() may cost here: no spinning, no waiting


def notes():
    return dict(farm_ws.take_link_notes())


@case
def test_a_slow_reader_gets_every_frame_in_order_and_never_holds_the_loop():
    with rig() as r:
        script = [200, "eagain", "eagain", 150, "eagain", 300, "eagain", 4096]
        ws, sock = r.client(script)
        msgs = [{"seq": n, "type": "stroke", "pad": "x" * 300} for n in range(3)]
        worst = max(r.timed(farm_ws.send, m) for m in msgs)
        worst = max(worst, r.polls(1.0))
        assert worst <= LOOP_SLICE, worst
        got = [farm_ws.json.loads(f)["seq"] for f in sock.frames()]
        assert got == [0, 1, 2], got
        assert ws in farm_ws._clients and farm_ws._path.state == "SEND_OK"
        assert farm_ws.stats["parked"] >= 1


@case
def test_a_client_that_stops_reading_is_cut_at_the_deadline_not_after_10s():
    with rig() as r:
        ws, sock = r.client(["eagain"])
        t0 = r.clock.t
        assert r.timed(farm_ws.send, {"seq": 1}) <= LOOP_SLICE     # parked, not spun on
        while ws in farm_ws._clients and r.clock.t - t0 < 10.0:
            assert r.timed(farm_ws.poll) <= LOOP_SLICE
            r.clock.t += 0.05
        cut_after = r.clock.t - t0
        assert ws not in farm_ws._clients, "never dropped"
        assert BOUND <= cut_after <= BOUND + 0.1, cut_after
        assert sock.data == bytearray() and sock.closes == 1       # no close frame queued
        d = notes()["client_dropped"]
        assert d["how"] == "cut NEW->overran" and "not taken" in d["reason"], d


@case
def test_every_write_is_non_blocking_and_at_most_one_mss():
    with rig() as r:
        ws, sock = r.client([4096])
        assert sock.timeout == 0.3                     # inherited from the listener
        farm_ws.send({"type": "status", "pad": "y" * 5000})
        assert sock.calls and all(not blocking for _, blocking in sock.calls), sock.calls
        assert max(n for n, _ in sock.calls) <= link_health.WS_CHUNK_B, sock.calls
        assert len(sock.calls) >= 4                    # 5 kB went out as several pbufs
        assert farm_ws.json.loads(sock.frames()[0])["pad"] == "y" * 5000


@case
def test_a_socket_left_blocking_cannot_hold_the_loop_for_its_timeout():
    with rig() as r:
        ws, sock = r.client(["eagain"])
        sock.setblocking(True)                          # as a library or fake might leave it
        took = r.timed(farm_ws.send, {"seq": 1})
        assert took <= LOOP_SLICE, took                 # set non-blocking before the write


@case
def test_handshake_reset_forgets_the_client_quietly():
    """10:41:42: accepted after the drain, the peer had given up: the
    library swallows ECONNRESET in the handshake, our first frame got EBADF
    and raised the backoff level.  Now: forgotten, nothing written, level 0."""
    with rig() as r:
        sock = FakeSocket(r.clock, [OSError(errno.EBADF, "EBADF")])

        class Srv:
            def poll(self):
                farm_ws.route_ws(types.SimpleNamespace(connection=sock, client_address=sock.peer))
                ws_bounds.last["resets"] += 1          # what bounded_send_bytes records
        farm_ws._server = Srv()
        farm_ws.send({"seq": 1})                        # an event to replay
        farm_ws.poll()
        assert not farm_ws._clients and sock.closes == 1 and not sock.calls
        assert farm_ws._path.state == "SEND_OK" and farm_ws._path.level == 0
        n = notes()
        assert "client_handshake_lost" in n and n["client_handshake_lost"]["peer"] == "192.168.0.22", n
        assert farm_ws.stats["handshakes_lost"] == 1


@case
def test_handshake_that_raises_forgets_the_client():
    with rig() as r:
        sock = FakeSocket(r.clock, [4096])

        class Srv:
            def poll(self):
                farm_ws.route_ws(types.SimpleNamespace(connection=sock, client_address=sock.peer))
                sock.close()                            # the library closes it, then raises
                raise OSError(errno.ETIMEDOUT, "send timeout 0.3 s")
        farm_ws._server = Srv()
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            farm_ws.poll()
        assert not farm_ws._clients and not sock.calls
        assert farm_ws.stats["handshakes_lost"] == 1


@case
def test_ebadf_on_a_new_clients_first_frame_is_gone_not_a_backoff():
    with rig() as r:
        farm_ws.send({"seq": 1})
        ws, sock = r.client([OSError(errno.EBADF, "EBADF")], replay=True)
        r.polls(0.2)
        assert ws not in farm_ws._clients and sock.closes == 1
        assert len(sock.calls) == 1                     # no close frame tried on a dead socket
        assert farm_ws._path.state == "SEND_OK" and farm_ws._path.level == 0
        d = notes()["client_dropped"]
        assert d["how"] == "close NEW->closed" and "gone" in d["reason"], d
        assert farm_ws.stats["gone"] == 1


@case
def test_close_never_raises_and_happens_once():
    with rig() as r:
        ws, sock = r.client([4096])
        sock.close_error = OSError(errno.EBADF, "EBADF")
        farm_ws._drop(ws, "test")
        farm_ws._close(ws, farm_ws.WS_GOING_AWAY, frame=False)
        farm_ws.drop_all("again")
        assert ws.closed and sock.closes == 1


@case
def test_no_close_frame_after_a_half_written_frame():
    with rig() as r:
        ws, sock = r.client([10, "eagain"])
        farm_ws.send({"seq": 1, "pad": "z" * 100})
        assert len(sock.data) == 10
        farm_ws._event(ws, "closed")                    # the table says: close politely
        assert len(sock.data) == 10 and sock.closes == 1    # but not into the middle of a frame


@case
def test_send_block_ms_setting_tightens_every_bound():
    with rig():
        assert link_health.SEND_BLOCK_MS == 400
        assert farm_ws.configure_bounds({}) == link_health.SEND_TIMEOUT_S
        assert farm_ws.configure_bounds({"link": {"send_block_ms": 200}}) == 0.2
        assert farm_ws.SEND_TIMEOUT_S == 0.2 and farm_ws.CLOSE_TIMEOUT_S == link_health.CLOSE_TIMEOUT_S
        assert ws_bounds.SEND_BOUND_S == ws_bounds.RECV_BOUND_S == ws_bounds.REQ_BOUND_S == 0.2
        assert farm_ws.configure_bounds({"link": {"send_block_ms": 5000}}) == link_health.SEND_TIMEOUT_S
        assert farm_ws.configure_bounds({"link": {"send_block_ms": 1}}) == link_health.MIN_SLICE_S
        assert farm_ws.configure_bounds({"link": {"send_block_ms": "x"}}) == link_health.SEND_TIMEOUT_S
        import json, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ex = json.load(open(os.path.join(root, "settings_example.json")))
        assert ex["link"] == {"send_block_ms": 400}, ex.get("link")
        sys.path.insert(0, os.path.join(root, "src"))
        import config_loader
        assert config_loader.DEFAULTS["link"] == {"send_block_ms": 400}


@case
def test_lwip_err_mem_sleep_is_the_one_wait_python_cannot_bound():
    """Documented, not hidden: lwip_tcp_send sleeps 200 x 50 ms inside one
    send() when tcp_write says ERR_MEM, socket timeout or not.  What Python
    does: cut that client, drain (no writes, no serving) so lwIP can free."""
    with rig() as r:
        ws, sock = r.client([("block", link_health.LWIP_ERR_MEM_S)])
        took = r.timed(farm_ws.send, {"seq": 1})
        assert took >= link_health.LWIP_ERR_MEM_S         # the C sleep: nothing to interrupt it
        assert ws not in farm_ws._clients
        assert farm_ws._path.state == link_health.SEND_DRAIN


finish()
