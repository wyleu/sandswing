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
    del farm_ws._clients[:]
    del farm_ws._refused[:]
    return [farm_ws._accept(FakeWs()) for _ in range(n)]


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


finish()
