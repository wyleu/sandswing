# tests for ws_bounds.py (deadlines on adafruit_httpserver's unbounded loops)
import errno
from support import case, finish
import ws_bounds


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.25           # every look at the clock costs 0.25 s
        return self.t


class Conn:
    def __init__(self, send=None, recv=None):
        self.send_script = list(send or [])
        self.recv_script = list(recv or [])
        self.sent = bytearray()

    def send(self, view):
        r = self.send_script.pop(0) if self.send_script else OSError(errno.EAGAIN, "again")
        if isinstance(r, Exception):
            raise r
        self.sent += bytes(view[:r])
        return r

    def recv_into(self, view):
        r = self.recv_script.pop(0) if self.recv_script else OSError(errno.EAGAIN, "again")
        if isinstance(r, Exception):
            raise r
        view[:len(r)] = r
        return len(r)


class Resp:
    _size = 0


class Ws:
    def __init__(self, conn):
        class Req:
            pass
        self._request = Req()
        self._request.connection = conn


def with_clock(fn):
    old = ws_bounds._now
    ws_bounds._now = Clock()
    try:
        return fn()
    finally:
        ws_bounds._now = old


@case
def test_send_to_a_peer_that_never_drains_gives_up():
    def go():
        try:
            ws_bounds.bounded_send_bytes(Resp(), Conn(), b"hello")
        except OSError as e:
            return e.errno
    assert with_clock(go) == errno.ETIMEDOUT


@case
def test_send_that_makes_progress_completes():
    c = Conn(send=[2, OSError(errno.EAGAIN, "again"), 3])
    r = Resp()
    ws_bounds.bounded_send_bytes(r, c, b"hello")
    assert bytes(c.sent) == b"hello" and r._size == 5


@case
def test_send_to_a_reset_peer_returns_quietly():
    ws_bounds.bounded_send_bytes(Resp(), Conn(send=[OSError(errno.ECONNRESET, "r")]), b"x")


@case
def test_no_frame_yet_is_still_eagain():
    try:
        ws_bounds.bounded_recv_exact(Ws(Conn()), bytearray(8), 2)
        assert False
    except OSError as e:
        assert e.errno == errno.EAGAIN


@case
def test_cut_off_frame_gives_up_and_is_not_swallowed():
    def go():
        try:
            ws_bounds.bounded_recv_exact(Ws(Conn(recv=[b"\x81"])), bytearray(8), 2)
        except OSError as e:
            return e.errno
    got = with_clock(go)
    assert got == ws_bounds.ECONNABORTED and got != errno.ETIMEDOUT


@case
def test_frame_that_arrives_in_pieces_is_read():
    c = Conn(recv=[b"\x81", OSError(errno.EAGAIN, "a"), b"\x05"])
    assert ws_bounds.bounded_recv_exact(Ws(c), bytearray(8), 2) == b"\x81\x05"


@case
def test_install_patches_both_classes():
    import types
    m = types.ModuleType("r")
    m.Response = type("Response", (), {"_send_bytes": None})
    m.Websocket = type("Websocket", (), {"_recv_exact": None})
    assert ws_bounds.install(m)
    assert m.Response._send_bytes is ws_bounds.bounded_send_bytes
    assert m.Websocket._recv_exact is ws_bounds.bounded_recv_exact
    assert not ws_bounds.install(types.ModuleType("empty"))


finish()
