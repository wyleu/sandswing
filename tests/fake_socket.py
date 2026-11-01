# fake_socket.py -- a CircuitPython socketpool socket for the bounded-send tests.
#
# Each send() follows a script, one entry per call (the last entry repeats):
#   int n      take at most n bytes (a short write when n < len)
#   "eagain"   raise EAGAIN (lwIP's send buffer is full)
#   ("block", s)  hold the caller s seconds inside the call, then take it all
#              (a blocking socket waiting for buffer space, or lwip_tcp_send's
#              ERR_MEM sleep, which no Python deadline can interrupt)
#   OSError    raise it (EBADF: the socket was shut under us; ECONNRESET)
# A blocking socket (setblocking(True) or a timeout) with a full buffer holds
# the caller for its timeout, like CircuitPython; non-blocking raises EAGAIN.
# The clock is any object with a float .t that the test also gives farm_ws.
import errno


class FakeSocket:
    def __init__(self, clock, script=(4096,), peer=("192.168.0.22", 50000)):
        self.clock = clock
        self.script = list(script)
        self.peer = peer
        self.data = bytearray()
        self.calls = []            # (bytes offered, blocking?) per send()
        self.timeout = 0.3         # what an accepted socket inherits (server.socket_timeout)
        self.closes = 0
        self.close_error = None

    # -- socket API ----------------------------------------------------------
    def setblocking(self, flag):
        self.timeout = None if flag else 0.0

    def settimeout(self, t):
        self.timeout = t

    def send(self, view):
        n_offered = len(view)
        self.calls.append((n_offered, self.timeout != 0.0))
        step = self.script[0] if len(self.script) == 1 else self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if step == "eagain":
            if self.timeout == 0.0:
                raise OSError(errno.EAGAIN, "EAGAIN")
            self.clock.t += self.timeout or 10.0       # a blocking socket waits for space
            raise OSError(errno.ETIMEDOUT, "ETIMEDOUT")
        if isinstance(step, tuple) and step[0] == "block":
            self.clock.t += step[1]
            step = n_offered
        n = min(int(step), n_offered)
        self.data.extend(bytes(view[:n]))
        return n

    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error

    # -- for the tests -------------------------------------------------------
    def frames(self):
        """Unmasked server text frames in self.data -> [payload str]."""
        out, i, d = [], 0, self.data
        while i + 2 <= len(d):
            n = d[i + 1] & 0x7F
            j = i + 2
            if n == 126:
                n = int.from_bytes(d[j:j + 2], "big")
                j += 2
            if j + n > len(d):
                break
            out.append(bytes(d[j:j + n]).decode())
            i = j + n
        return out
