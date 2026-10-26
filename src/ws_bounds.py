# ws_bounds.py -- put a deadline on the places where adafruit_httpserver
# (4.8.2, and 4.7.1 which is what the Pico has) can wait for ever, freezing the whole Pico (sensing, heartbeats,
# every task) until a peer happens to close:
#   * Response._send_bytes: `except EAGAIN: continue` with no limit, used for
#     the /status body and the /ws handshake reply;
#   * Websocket._recv_exact: once the first bytes of a frame have arrived it
#     retries EAGAIN with no limit, so a frame cut off by a Wi-Fi hiccup holds
#     the loop until the TCP connection times out (seen: 41 s and 140 s
#     freezes, ending only when the clients were dropped).
#   * 4.7.1 has no _recv_exact: Websocket._read_frame reads the payload in a
#     `while 0 < length` loop, which spins for ever when the peer has closed
#     (recv_into returns 0), and a frame cut off by EAGAIN loses its first
#     bytes so every later frame is misread.  bounded_read_frame replaces it.
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
                raise OSError(ENOTCONN, "peer closed")
            break
        received += count
    return bytes(view[:received])


MAX_FRAME_BYTES = 8192      # our clients send small text frames; more is garbage


def _read_n(conn, buf, n, deadline):
    """Exactly n bytes, retrying EAGAIN until the deadline; 0 bytes = peer closed."""
    got = 0
    view = memoryview(buf)
    while got < n:
        try:
            c = conn.recv_into(view[got:n])
        except OSError as exc:
            if exc.errno == EAGAIN:
                if _now() > deadline:
                    raise OSError(ECONNABORTED, "partial frame timeout %.1f s" % RECV_BOUND_S)
                continue
            raise
        if c == 0:
            raise OSError(ENOTCONN, "peer closed")
        got += c
    return bytes(view[:n])


def bounded_read_frame(self):
    """adafruit_httpserver 4.7.1 Websocket._read_frame with deadlines.  Nothing
    waiting raises EAGAIN (the library's receive() turns that into None); a
    closed peer raises ENOTCONN (receive() closes the socket); a frame that
    stalls part-way, or is absurdly long, raises ECONNABORTED so the client is
    dropped rather than left out of step with the stream."""
    conn = self._request.connection
    buf = bytearray(self._buffer_size if self._buffer_size > 16 else 16)
    first = conn.recv_into(buf, 2)              # EAGAIN here = no frame yet
    if first == 0:
        raise OSError(ENOTCONN, "peer closed")
    deadline = _now() + RECV_BOUND_S
    header = buf[:first]
    if first < 2:
        header = header + _read_n(conn, buf, 1, deadline)
    fin, opcode, has_mask, length = self._parse_frame_header(header)
    # continuation frames are not supported by the library either
    if fin != self.FIN and opcode == self.CONT:
        return self.CONT, None
    if fin == self.FIN and opcode == self.CLOSE:
        return self.CLOSE, b""
    if length < 0:
        length = int.from_bytes(_read_n(conn, buf, -length, deadline), "big")
    if length > MAX_FRAME_BYTES:
        raise OSError(ECONNABORTED, "frame of %d bytes" % length)
    mask = _read_n(conn, buf, 4, deadline) if has_mask else None
    payload = bytearray()
    while length > 0:
        n = length if length < len(buf) else len(buf)
        payload += _read_n(conn, buf, n, deadline)
        length -= n
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    else:
        payload = bytes(payload)
    return opcode, payload


def install(response_module):
    """Patch the library classes in place.  True if both were patched."""
    ok = 0
    resp = getattr(response_module, "Response", None)
    if resp is not None and hasattr(resp, "_send_bytes"):
        resp._send_bytes = bounded_send_bytes
        ok += 1
    ws = getattr(response_module, "Websocket", None)
    if ws is not None and hasattr(ws, "_recv_exact"):          # 4.8.x
        ws._recv_exact = bounded_recv_exact
        ok += 1
    elif ws is not None and hasattr(ws, "_read_frame"):        # 4.7.x
        ws._read_frame = bounded_read_frame
        ok += 1
    return ok == 2
