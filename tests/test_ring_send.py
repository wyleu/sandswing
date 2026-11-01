# tests for the network while the bells ring (link_health.NetMode,
# settings.json link.ring_send live / batched / shed): MIDI first.
import io
import contextlib
import json
import sys
import types

from support import case, finish

for name in ("wifi", "adafruit_connection_manager"):
    sys.modules.setdefault(name, types.ModuleType(name))
_http = types.ModuleType("adafruit_httpserver")


class Ws:
    """The Websocket surface farm_ws writes through (4.8.2 internals)."""
    def __init__(self, conn, peer="192.168.0.22"):
        self.closed = False
        self.on_receive = []
        self._request = types.SimpleNamespace(connection=conn, client_address=(peer, 1))

    @staticmethod
    def _prepare_frame(opcode, message):
        if len(message) < 126:
            return bytearray([0x80 | opcode, len(message)]) + message
        return bytearray([0x80 | opcode, 126]) + len(message).to_bytes(2, "big") + message

    def receive(self, fail_silently=False):
        return self.on_receive.pop(0) if self.on_receive else None

    def close(self, code=None):
        self.closed = True


for attr in ("Server", "Request", "Response", "GET"):
    setattr(_http, attr, object)
_http.Websocket = Ws
sys.modules["adafruit_httpserver"] = _http

import farm_ws
import link_health as L


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Conn:
    """Counts socket.send() calls: each one asks lwIP for (at most) one
    new segment, i.e. one ~1.5 kB pbuf held until it is ACKed."""
    def __init__(self):
        self.data = bytearray()
        self.calls = []
        self.shut = False

    def send(self, view):
        self.calls.append(len(view))
        self.data.extend(bytes(view))
        return len(view)

    def setblocking(self, flag):
        pass

    def close(self):
        self.shut = True


class CountServer:
    def __init__(self):
        self.polls = 0

    def poll(self):
        self.polls += 1


def frames(data):
    """Text frames (unmasked, server -> client) in a byte stream."""
    out, i = [], 0
    while i < len(data):
        n = data[i + 1] & 0x7F
        j = i + 2
        if n == 126:
            n = int.from_bytes(data[j:j + 2], "big")
            j += 2
        out.append(bytes(data[j:j + n]).decode())
        i = j + n
    return out


def stroke(seq):
    return {"schema": "sand.event/v1", "type": "stroke", "src": "sandswing",
            "id": "concentrator", "boot_id": "7091cdab", "seq": seq,
            "tick_ms": 123456789, "utc_ms": 1793800000000, "err_ms": 12,
            "sync": "synced", "acc": "ok", "epoch": 3, "session": 7,
            "mode": "ringing", "srv": "10.42.0.1", "stratum": 1, "body": {"ch": 8}}


class rig:
    """farm_ws with one heard client, a fake clock and a ring_send mode."""
    def __init__(self, mode, quiet_s=L.RING_QUIET_S, batch_ms=None):
        self.mode, self.quiet_s, self.batch_ms = mode, quiet_s, batch_ms

    def __enter__(self):
        farm_ws.reset_state()
        link = {"ring_send": self.mode, "ring_quiet_s": self.quiet_s}
        if self.batch_ms is not None:
            link["batch_ms"] = self.batch_ms
        farm_ws.configure_ring({"link": link})
        self.clock = Clock()
        self.real = farm_ws.time.monotonic
        farm_ws.time.monotonic = self.clock
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()
        self.conn = Conn()
        self.ws = farm_ws._accept(Ws(self.conn))
        farm_ws._new.remove(self.ws)
        self.ws.on_receive = ['{"type":"heartbeat","src":"sandbells"}']
        farm_ws.poll()
        return self

    def polls(self, seconds, step=0.05, talk=True):
        end = self.clock.t + seconds
        while self.clock.t < end:
            if talk and self.ws in farm_ws._clients and not self.ws.on_receive:
                self.ws.on_receive = ['{"type":"heartbeat"}']
            farm_ws.poll()
            self.clock.t += step

    def __exit__(self, *a):
        self.out.__exit__(*a)
        farm_ws.time.monotonic = self.real
        farm_ws._server = None
        farm_ws.configure_ring(None)


@case
def test_net_plan_covers_every_state_and_mode_and_quiet_is_as_before():
    for st in (L.N_QUIET, L.N_RINGING):
        for mode in L.RING_SEND_MODES:
            assert (st, mode) in L.NET_PLAN
    for mode in (L.RING_SEND_LIVE, L.RING_SEND_BATCHED, L.RING_SEND_SHED):
        assert L.NET_PLAN[(L.N_QUIET, mode)] == (True, True, True, True)
    # live while ringing: written as before, but no Wi-Fi join (it blocks MIDI)
    assert L.NET_PLAN[(L.N_RINGING, L.RING_SEND_LIVE)] == (True, True, True, False)
    for mode in L.RING_SEND_MODES:
        assert L.NET_PLAN[(L.N_RINGING, mode)][3] is False
    assert L.RING_SEND == L.RING_SEND_LIVE           # default: written as before


@case
def test_off_is_midi_only_from_boot_whatever_the_strokes():
    n = L.NetMode(L.RING_SEND_OFF)
    for t in (0.0, L.RING_QUIET_S * 2):
        assert not (n.inline() or n.writes() or n.serves() or n.rejoins()) and n.off()
        n.stroke(t)
    with rig("off") as r:
        before = len(r.conn.data)
        farm_ws.send(stroke(1))
        r.polls(1.0)
        assert len(r.conn.data) == before and farm_ws.net_off()


@case
def test_ringing_is_a_stroke_within_ring_quiet_s_not_the_tower_state():
    n = L.NetMode(L.RING_SEND_SHED, L.BATCH_PERIOD_S, L.RING_QUIET_S)
    assert n.stroke(0.0) == "ring" and n.ringing()
    assert n.stroke(L.SLOWEST_TOLL_S) is None        # a slow toll: still ringing
    assert n.tick(L.SLOWEST_TOLL_S + L.RING_QUIET_S - 1) is None
    assert n.tick(L.SLOWEST_TOLL_S + L.RING_QUIET_S) == "calm" and not n.ringing()


@case
def test_ring_settings_defaults_and_bad_values():
    assert L.ring_settings(None) == (L.RING_SEND, L.BATCH_PERIOD_S, L.RING_QUIET_S)
    m, b, q = L.ring_settings({"link": {"ring_send": "loud", "batch_ms": "x", "ring_quiet_s": -5}})
    assert (m, b, q) == (L.RING_SEND, L.BATCH_PERIOD_S, 0.0)
    m, b, q = L.ring_settings({"link": {"ring_send": "batched", "batch_ms": 250}})
    assert m == "batched" and b == 0.25


@case
def test_live_writes_each_stroke_at_once_as_before():
    with rig("live") as r:
        before = len(r.conn.calls)
        for q in range(1, 4):
            farm_ws.send(stroke(q))
        assert len(r.conn.calls) - before == 3       # one write (one segment) per stroke
        assert farm_ws.net_ringing()


@case
def test_batched_never_writes_from_send_and_puts_four_strokes_in_one_segment():
    with rig("batched") as r:
        before = len(r.conn.calls)
        for q in range(1, 5):
            farm_ws.send(stroke(q))
        assert len(r.conn.calls) == before           # nothing from the sense path
        r.polls(0.1)
        new = r.conn.calls[before:]
        assert len(new) == 1 and new[0] <= L.BATCH_MAX_B, new
        got = [json.loads(f)["seq"] for f in frames(r.conn.data)[-4:]]
        assert got == [1, 2, 3, 4], got               # same frames, same seq, in order
        assert farm_ws.stats["batches"] == 1 and farm_ws.stats["batched_frames"] == 4
        r.polls(0.1)                                 # heard after a whole write: confirmed
        assert farm_ws.undelivered_strokes() == []


@case
def test_batched_writes_once_per_period_and_a_fifth_stroke_waits():
    with rig("batched") as r:
        before = len(r.conn.calls)
        for q in range(1, 7):                         # 6 x ~320 B: more than one segment
            farm_ws.send(stroke(q))
        r.polls(0.1)
        assert len(r.conn.calls) - before == 1        # 4 now ...
        r.polls(L.BATCH_PERIOD_S - 0.3)
        assert len(r.conn.calls) - before == 1        # ... nothing inside the period ...
        r.polls(0.4)
        assert len(r.conn.calls) - before == 2        # ... the rest one period later
        assert [json.loads(f)["seq"] for f in frames(r.conn.data)[-6:]] == [1, 2, 3, 4, 5, 6]


@case
def test_batched_heartbeat_waits_for_the_batch_too():
    with rig("batched") as r:
        farm_ws.send(stroke(1))
        before = len(r.conn.calls)
        farm_ws.send({"type": "heartbeat", "pad": "x" * 1500}, keep=False)
        assert len(r.conn.calls) == before
        r.polls(2 * L.BATCH_PERIOD_S)
        assert '"heartbeat"' in frames(r.conn.data)[-1]


@case
def test_shed_cuts_writes_and_serves_nothing_while_ringing_then_replays_every_stroke():
    with rig("shed", quiet_s=L.RING_QUIET_S) as r:
        srv = farm_ws._server = CountServer()
        r.polls(0.1)
        assert srv.polls > 0
        written = len(r.conn.data)
        farm_ws.send(stroke(1))                        # first stroke: the network gives way
        assert r.ws not in farm_ws._clients and r.conn.shut
        assert len(r.conn.data) == written             # cut: not even a close frame
        assert farm_ws.net_ringing() and not farm_ws.net_rejoins()
        polls0 = srv.polls
        for q in range(2, 101):                        # the funeral: 100 tolls ...
            farm_ws.send(stroke(q))
            farm_ws.send({"type": "heartbeat"}, keep=False)
            r.polls(0.2, talk=False)
        assert srv.polls == polls0                     # ... nothing served or written
        assert len(farm_ws.undelivered_strokes()) == farm_ws.STROKE_KEEP   # oldest 36 gone
        assert farm_ws.stats["strokes_dropped_unconfirmed"] == 100 - farm_ws.STROKE_KEEP
        r.polls(L.RING_QUIET_S + 0.1, step=0.5, talk=False)
        assert not farm_ws.net_ringing() and srv.polls > polls0     # served again
        notes = [w for w, _ in farm_ws.take_link_notes()]
        assert "net_ringing" in notes and "net_quiet" in notes, notes
        conn2 = Conn()
        ws2 = farm_ws._accept(Ws(conn2))               # Sandbells reconnects
        ws2.on_receive = ['{"type":"heartbeat","src":"sandbells"}']
        r.ws = ws2
        r.polls(30.0)
        seqs = [json.loads(f)["seq"] for f in frames(conn2.data) if '"stroke"' in f]
        assert seqs == list(range(100 - farm_ws.STROKE_KEEP + 1, 101)), seqs[:3]


@case
def test_shed_keeps_strokes_for_a_reset_hold():
    with rig("shed") as r:
        farm_ws.send(stroke(1))
        farm_ws.send(stroke(2))
        assert len(farm_ws.undelivered_strokes()) == 2
        assert L.reset_decision(0.0, 10.0, len(farm_ws.undelivered_strokes()), False) == "hold"


@case
def test_client_diagram_lists_the_shed_cut():
    for st in (L.C_NEW, L.C_LIVE, L.C_LAGGING):
        assert L.CLIENT_TRANSITIONS[(st, "shed")] == (L.C_GONE, L.CUT)


finish()
