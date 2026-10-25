# ws_bounds.py -- put a deadline on the two places where adafruit_httpserver
# 4.8.2 can wait for ever, freezing the whole Pico (sensing, heartbeats,
# every task) until a peer happens to close:
#   * Response._send_bytes: `except EAGAIN: continue` with no limit, used for
#     the /status body and the /ws handshake reply;
#   * Websocket._recv_exact: once the first bytes of a frame have arrived it
#     retries EAGAIN with no limit, so a frame cut off by a Wi-Fi hiccup holds
#     the loop until the TCP connection times out (seen: 41 s and 140 s
#     freezes, ending only when the clients were dropped).
# A bounded wait raises OSError; farm_ws.poll() drops that one client and
# carries on.  Runs on CPython (tests) and CircuitPython.
import time

try:
    from errno import EAGAIN, ECONNRESET, ENOTCONN, ETIMEDOUT
except ImportError:                      # pragma: no cover
    EAGAIN, ECONNRESET, ENOTCONN, ETIMEDOUT = 11, 104, 107, 110
try:
    from errno import ECONNABORTED
except ImportError:                      # pragma: no cover
    ECONNABORTED = 103

SEND_BOUND_S = 1.5          # most a library send may wait on a full TCP buffer
RECV_BOUND_S = 1.0          # most the rest of a started frame may take

_now = time.monotonic       # tests replace this


def bounded_send_bytes(self, conn, buffer):
    sent = 0
    total = len(buffer)
    view = memoryview(buffer)
    deadline = _now() + SEND_BOUND_S
    while sent < total:
        try:
            n = conn.send(view[sent:])
        except OSError as exc:
            if exc.errno == EAGAIN:
                if _now() > deadline:
                    raise OSError(ETIMEDOUT, "send timeout %.1f s" % SEND_BOUND_S)
                continue
            if exc.errno == ECONNRESET:
                return
            raise
        if n:
            sent += n
        elif _now() > deadline:
            raise OSError(ETIMEDOUT, "send timeout %.1f s" % SEND_BOUND_S)
    self._size += sent


def bounded_recv_exact(self, buffer, size):
    received = 0
    view = memoryview(buffer)
    deadline = None
    while received < size:
        remaining = size - received
        try:
            count = self._request.connection.recv_into(view[received:received + remaining])
        except OSError as error:
            if error.errno == EAGAIN and received == 0:
                raise                    # nothing started: normal "no frame yet"
            if error.errno == EAGAIN:
                if deadline is None:
                    deadline = _now() + RECV_BOUND_S
                elif _now() > deadline:
                    # a cut-off frame leaves the stream out of step: not ETIMEDOUT
                    # (the library would swallow that), so the client is dropped
                    raise OSError(ECONNABORTED, "partial frame timeout %.1f s" % RECV_BOUND_S)
                continue
            raise
        if count == 0:
            if received == 0:
                raise OSError(ENOTCONN)
            break
        received += count
    return bytes(view[:received])


def install(response_module):
    """Patch the library classes in place.  True if both were patched."""
    ok = 0
    resp = getattr(response_module, "Response", None)
    if resp is not None and hasattr(resp, "_send_bytes"):
        resp._send_bytes = bounded_send_bytes
        ok += 1
    ws = getattr(response_module, "Websocket", None)
    if ws is not None and hasattr(ws, "_recv_exact"):
        ws._recv_exact = bounded_recv_exact
        ok += 1
    return ok == 2
