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

    def recv_into(self, view, nbytes=None):
        r = self.recv_script.pop(0) if self.recv_script else OSError(errno.EAGAIN, "again")
        if isinstance(r, Exception):
            raise r
        n = min(len(r), len(view), nbytes if nbytes is not None else len(view))
        view[:n] = r[:n]
        if n < len(r):
            self.recv_script.insert(0, r[n:])      # the rest is still queued
        return n


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


class Lib47:
    """just enough of adafruit_httpserver 4.7.1 Websocket for _read_frame"""
    FIN = 0x80
    CONT = 0
    CLOSE = 8
    _buffer_size = 1024

    def __init__(self, conn):
        class Req:
            pass
        self._request = Req()
        self._request.connection = conn

    @staticmethod
    def _parse_frame_header(header):
        fin = header[0] & 0x80
        opcode = header[0] & 0x0F
        has_mask = header[1] & 0x80
        length = header[1] & 0x7F
        if length == 126:
            length = -2
        elif length == 127:
            length = -8
        return fin, opcode, has_mask, length


def read(frames):
    return ws_bounds.bounded_read_frame(Lib47(Conn(recv=frames)))


@case
def test_frame_read_whole_text_frame():
    assert read([b"\x81\x05", b"hello"]) == (1, b"hello")


@case
def test_frame_read_masked_frame():
    key = b"\x01\x02\x03\x04"
    body = bytes(b ^ key[i % 4] for i, b in enumerate(b"hi!!!"))
    assert read([b"\x81\x85", key, body]) == (1, b"hi!!!")


@case
def test_frame_read_nothing_waiting_is_eagain():
    try:
        read([])
        assert False
    except OSError as e:
        assert e.errno == errno.EAGAIN


@case
def test_frame_read_peer_closed_before_header():
    try:
        read([b""])
        assert False
    except OSError as e:
        assert e.errno == errno.ENOTCONN


@case
def test_frame_read_peer_closed_mid_payload_does_not_spin():
    # the library loops for ever here (recv_into returns 0, length never falls)
    try:
        read([b"\x81\x05", b"he", b""])
        assert False
    except OSError as e:
        assert e.errno == errno.ENOTCONN


@case
def test_frame_read_stalled_payload_gives_up():
    def go():
        try:
            read([b"\x81\x05", b"he"])
        except OSError as e:
            return e.errno
    assert with_clock(go) == ws_bounds.ECONNABORTED


@case
def test_frame_read_oversize_frame_is_refused():
    try:
        read([b"\x81\x7e", b"\xff\xff"])
        assert False
    except OSError as e:
        assert e.errno == ws_bounds.ECONNABORTED


@case
def test_frame_read_close_frame():
    assert read([b"\x88\x00"]) == (8, b"")


@case
def test_frame_read_one_byte_header_then_rest():
    assert read([b"\x81", b"\x02", b"ok"]) == (1, b"ok")


@case
def test_install_on_4_7_library_patches_read_frame():
    import types
    m = types.ModuleType("r")
    m.Response = type("Response", (), {"_send_bytes": None})
    m.Websocket = type("Websocket", (), {"_read_frame": None})
    assert ws_bounds.install(m)
    assert m.Websocket._read_frame is ws_bounds.bounded_read_frame


finish()
