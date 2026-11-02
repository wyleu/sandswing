# tests for the network while the bells ring (link_health.NetMode,
# settings.json link.ring_send live / batched / shed): MIDI first.
import io
import contextlib
import json
import os
import sys
import types

from support import case, finish

# set: the worst-case slim heartbeat is written there as slim_hb.json (Sandbells check)
TEST_OUT = os.environ.get("SANDSWING_TEST_OUT")

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


class Pool:
    AF_INET, SOCK_DGRAM = 2, 2

    def __init__(self, fail=None):
        self.fail, self.sent, self.socks = fail, [], 0

    def socket(self, fam, typ):
        self.socks += 1
        pool = self

        class S:
            def setblocking(self, flag):
                pass

            def sendto(self, data, dest):
                if pool.fail:
                    raise OSError(pool.fail, "ENOMEM")
                pool.sent.append((bytes(data), dest))
                return len(data)

            def close(self):
                pass
        return S()


@case
def test_udp_tier_sends_each_stroke_as_one_datagram_and_never_tcp():
    with rig("udp") as r:
        pool = Pool()
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        before = len(r.conn.data)
        for q in (1, 2, 3):
            assert farm_ws.send(stroke(q))
        farm_ws.send({"type": "heartbeat"}, keep=False)
        r.polls(1.0)
        assert len(r.conn.data) == before                 # no TCP write at all
        assert [json.loads(d)["seq"] for d, _ in pool.sent[:3]] == [1, 2, 3]
        assert all(dest == ("10.42.0.50", L.UDP_PORT) for _, dest in pool.sent)
        assert json.loads(pool.sent[0][0])["utc_ms"] == 1793800000000   # its own timestamp
        assert not farm_ws.net_tcp() and farm_ws.net_udp()
        assert not L.NET_PLAN[(L.N_RINGING, L.RING_SEND_UDP)][3]          # no join while ringing
        assert L.NET_PLAN[(L.N_QUIET, L.RING_SEND_UDP)][3]


@case
def test_udp_error_is_a_counted_drop_not_a_wait_and_the_next_one_tries_again():
    with rig("udp"):
        pool = Pool(fail=12)
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50", "udp_port": 6000}}, pool)
        assert not farm_ws.send(stroke(1))
        assert farm_ws.stats["udp_dropped"] == 1
        pool.fail = None
        assert farm_ws.send(stroke(2)) and pool.sent[0][1] == ("10.42.0.50", 6000)
        assert pool.socks == 2                            # a fresh socket after the error
        assert farm_ws.stats["udp_sent"] == 1


@case
def test_udp_never_fragments_and_without_a_host_sends_nothing():
    with rig("udp"):
        pool = Pool()
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        assert not farm_ws.send({"type": "heartbeat", "pad": "x" * L.UDP_MAX_B}, keep=False)
        assert farm_ws.stats["udp_too_big"] == 1 and not pool.sent
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            farm_ws.stats["udp_too_big"] = 0
            farm_ws.send({"type": "heartbeat", "pad": "x" * L.UDP_MAX_B}, keep=False)
            farm_ws.send({"type": "heartbeat", "pad": "x" * L.UDP_MAX_B}, keep=False)
        assert out.getvalue().count("udp too big, dropped") == 1, out.getvalue()
        farm_ws.configure_udp({"link": {}}, pool)
        assert not farm_ws.send(stroke(1)) and not pool.sent


@case
def test_udp_host_list_picks_the_sandbells_on_our_subnet_home_or_tower():
    hosts = ["192.168.0.221", "10.42.0.1"]          # one per site, no other field
    w = farm_ws.wifi
    had = getattr(w, "radio", None)
    try:
        with rig("udp"):
            pool = Pool()
            w.radio = types.SimpleNamespace(ipv4_address="10.42.0.81", ipv4_subnet="255.255.255.0")
            assert farm_ws.configure_udp({"link": {"udp_host": hosts}}, pool) == ("10.42.0.1", L.UDP_PORT)
            w.radio = types.SimpleNamespace(ipv4_address="192.168.0.81", ipv4_subnet="255.255.255.0")
            assert farm_ws.send(stroke(1))           # a fresh socket picks again
            assert pool.sent[-1][1] == ("192.168.0.221", L.UDP_PORT)
            w.radio = types.SimpleNamespace(ipv4_address=None, ipv4_subnet=None)
            assert farm_ws.configure_udp({"link": {"udp_host": "192.168.0.221, 10.42.0.1"}},
                                         pool) == ("192.168.0.221", L.UDP_PORT)   # no IP: first
    finally:
        if had is None:
            del w.radio
        else:
            w.radio = had


class CmdPool(Pool):
    """Pool whose sockets also take bind() and recvfrom_into() (non-blocking:
    EAGAIN when nothing is waiting, as Socket.c lwip_raw_udp_receive)."""
    def __init__(self):
        Pool.__init__(self)
        self.inbox = []
        self.bound = []

    def socket(self, fam, typ):
        s = Pool.socket(self, fam, typ)
        pool = self

        def bind(addr):
            pool.bound.append(addr)

        def recvfrom_into(buf):
            if not pool.inbox:
                raise OSError(farm_ws.EAGAIN, "EAGAIN")
            data, peer = pool.inbox.pop(0)
            n = min(len(buf), len(data))
            buf[:n] = data[:n]
            return len(data), peer
        s.bind, s.recvfrom_into = bind, recvfrom_into
        return s


UID = "7091CDAB9DBD5305"
SB = "10.42.0.50"


def cmd(seq, **kw):
    d = {"schema": L.CMD_SCHEMA, "cmd": L.CMD_WIFI_OFF, "uid": UID,
         "boot_id": "7091cdab", "seq": seq}
    d.update(kw)
    return json.dumps(d).encode()


class udp_rig(rig):
    def __enter__(self):
        rig.__enter__(self)
        self.pool = CmdPool()
        farm_ws.configure_udp({"link": {"udp_host": SB}}, self.pool)
        self.real_uid = farm_ws._device_uid
        farm_ws._device_uid = lambda: UID
        farm_ws.send(stroke(1))                  # this boot's boot_id is known
        return self

    def __exit__(self, *a):
        farm_ws._device_uid = self.real_uid
        rig.__exit__(self, *a)


@case
def test_wifi_off_command_is_acked_while_still_udp_then_off_until_reboot():
    with udp_rig("udp") as r:
        r.polls(0.2)
        assert r.pool.bound == [("0.0.0.0", L.CMD_PORT)]
        r.pool.inbox.append((cmd(5), (SB, 40000)))
        farm_ws.poll()                                       # received: noted, not applied
        assert farm_ws.net_udp()
        notes = dict(farm_ws.take_link_notes())
        assert notes["net_command"]["cmd_seq"] == 5 and notes["net_command"]["then"] == "off"
        assert farm_ws.send({"type": "link", "boot_id": "7091cdab", "seq": 2})   # the ack goes out
        farm_ws.poll()                                       # applied now
        assert farm_ws.net_off() and not farm_ws.net_udp()
        st = farm_ws.link_status()
        assert st["ring_send"] == "off" and st["ring_send_boot"] == "udp"
        n = len(r.pool.sent)
        assert not farm_ws.send(stroke(3)) and len(r.pool.sent) == n   # nothing more is sent
        r.pool.inbox.append((cmd(6), (SB, 40000)))
        r.polls(0.5)
        assert r.pool.inbox                                  # off: not even read any more
        farm_ws.configure_ring({"link": {"ring_send": "udp"}})   # the next boot: default again
        assert farm_ws.net_udp()


@case
def test_radio_goes_down_only_when_the_tower_is_quiet():
    with udp_rig("udp") as r:
        farm_ws.set_tower("active")
        r.pool.inbox.append((cmd(1), (SB, 1)))
        farm_ws.poll()
        farm_ws.poll()
        assert farm_ws.net_off() and not farm_ws.radio_off_due()     # stroke just made, tower active
        r.clock.t += L.RING_QUIET_S + 1
        farm_ws.poll()
        assert not farm_ws.radio_off_due()                           # tower still active
        farm_ws.set_tower("inactive")
        assert farm_ws.radio_off_due()


@case
def test_commands_from_anyone_else_or_replayed_or_for_another_boot_are_refused():
    with udp_rig("udp") as r:
        bad = [(cmd(1), ("192.168.0.99", 1)),                  # not link.udp_host
               (cmd(1, uid="0F8A68C934D3A61C"), (SB, 1)),     # the sandtest Pico's uid
               (cmd(1, boot_id="beefbeef"), (SB, 1)),         # an earlier boot
               (cmd(1, cmd="wifi_on"), (SB, 1)),              # not in MODE_COMMANDS
               (b"x" * (L.CMD_MAX_B + 10), (SB, 1)),          # too long
               (b"not json", (SB, 1))]
        for item in bad:
            r.pool.inbox.append(item)
            farm_ws.poll()
        assert farm_ws.net_udp() and farm_ws._cmd["refused"] == len(bad)
        r.pool.inbox.append((cmd(7), (SB, 1)))
        farm_ws.poll()
        assert farm_ws._cmd["last_seq"] == 7
        farm_ws._cmd["pending"] = None                         # (not applied: test the seq rule)
        r.pool.inbox.append((cmd(7), (SB, 1)))
        farm_ws.poll()
        assert "replay" in farm_ws._cmd["last_why"]


@case
def test_cmd_check_is_a_pure_table():
    ok = dict(schema=L.CMD_SCHEMA, cmd=L.CMD_WIFI_OFF, uid="U", boot_id="B", seq=3)
    assert L.cmd_check(ok, "h", "h", "U", "B", None) == (True, None)
    assert not L.cmd_check(ok, "h", None, "U", "B", None)[0]   # no udp_host: nobody may
    assert not L.cmd_check(ok, "h", "h", "U", "B", 3)[0]
    n = L.NetMode(L.RING_SEND_UDP)
    assert n.command(L.CMD_WIFI_OFF) == L.RING_SEND_OFF and n.boot_mode == L.RING_SEND_UDP
    assert n.command(L.CMD_WIFI_OFF) is None                   # no way back on



# --- diag: heartbeat UDP sends and udp stats (log only) ---------------------
def hb_doc(seq):
    return {"schema": "sand.panel/v1", "type": "heartbeat", "src": "sandswing",
            "boot_id": "7091cdab", "seq": seq, "body": {"pad": "x" * 900}}


@case
def test_hb_diag_logs_the_first_three_heartbeats_with_size_gate_and_udp_result():
    with rig("udp"):
        pool = Pool()
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        d, lines = L.HeartbeatDiag(0.0), []
        seq = [0]

        def build():
            seq[0] += 1
            return hb_doc(seq[0])
        for t in (5.0, 10.0, 15.0, 20.0):
            assert d.gate(t, farm_ws.ws_clients(), farm_ws.net_udp(), lines.append)
            assert d.heartbeat(t, build, lambda doc: farm_ws.send(doc, keep=False), lines.append)
        size = len(json.dumps(hb_doc(1)).encode("utf-8"))
        assert size < L.UDP_MAX_B
        assert lines == ["%.3f hb #%d: %d B, ws=1 udp=1, sent=True" % (t, n, size)
                         for n, t in ((1, 5.0), (2, 10.0), (3, 15.0))], lines
        assert d.built == 4 and len(pool.sent) == 4
        assert json.loads(pool.sent[3][0])["type"] == "heartbeat"


@case
def test_hb_diag_says_when_the_gate_is_closed_for_the_first_three_loops_only():
    d, lines = L.HeartbeatDiag(0.0), []
    for t in (5.0, 10.0, 15.0, 20.0):
        assert not d.gate(t, 0, False, lines.append)
    assert lines == ["%.3f hb gate closed, loop %d: ws=0 udp=0" % (t, n)
                     for n, t in ((1, 5.0), (2, 10.0), (3, 15.0))], lines
    d2, lines2 = L.HeartbeatDiag(0.0), []
    for t in (5.0, 10.0, 15.0):
        d2.gate(t, 1, False, lines2.append)
    assert not d2.gate(20.0, 0, False, lines2.append) and lines2 == []   # loop 4: too late to log


@case
def test_hb_diag_prints_any_exception_with_its_type_and_never_raises():
    d, lines, errs = L.HeartbeatDiag(0.0), [], []

    def bad_doc():
        raise TypeError("object of type 'int' has no len()")

    def bad_send(doc):
        raise OSError(12, "ENOMEM")
    d.gate(5.0, 0, True, lines.append)
    assert d.heartbeat(5.0, bad_doc, bad_send, lines.append, errs.append) is None
    assert errs == ["5.000 hb build failed: TypeError: object of type 'int' has no len()"], errs
    assert d.built == 0
    assert d.heartbeat(10.0, lambda: hb_doc(1), bad_send, lines.append, errs.append) is None
    assert errs[1].startswith("10.000 hb #1 send failed: OSError: ") and "ENOMEM" in errs[1], errs
    assert lines == []                                     # no "sent=" line for a failed send
    d.heartbeat(15.0, lambda: {"x": object()}, lambda doc: False, lines.append, errs.append)
    assert errs[2].startswith("15.000 hb #2 size failed: TypeError: "), errs
    assert lines == ["15.000 hb #2: -1 B, ws=0 udp=1, sent=False"], lines


@case
def test_hb_diag_udp_stats_line_every_60_s_from_farm_ws_stats():
    with rig("udp"):
        pool = Pool()
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        d, lines = L.HeartbeatDiag(0.0), []
        t = 0.0
        while t < 2 * L.UDP_STATS_S:
            t += 5.0
            d.gate(t, 0, True, lines.append)
            d.heartbeat(t, lambda: hb_doc(1), lambda doc: farm_ws.send(doc, keep=False),
                        lambda s: None)
            d.stats(t, farm_ws.stats, lines.append)
        farm_ws.send({"type": "heartbeat", "pad": "x" * L.UDP_MAX_B}, keep=False)
        pool.fail = 12
        farm_ws.send(stroke(1))
        assert d.stats(t + L.UDP_STATS_S, farm_ws.stats, lines.append)
        assert lines == ["60.000 udp stats: hb=12 sent=12 dropped=0 too_big=0 slow=0 max_ms=0",
                         "120.000 udp stats: hb=24 sent=24 dropped=0 too_big=0 slow=0 max_ms=0",
                         "180.000 udp stats: hb=24 sent=24 dropped=1 too_big=1 slow=0 max_ms=0"], lines
        for k in ("udp_sent", "udp_dropped", "udp_too_big", "udp_slow", "udp_max_ms"):
            assert k in farm_ws.stats


@case
def test_udp_send_says_why_on_the_first_drop_and_every_100th():
    with rig("udp"):
        pool = Pool(fail=12)
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for q in range(L.UDP_DROP_LOG_EVERY + 1):
                assert not farm_ws.send(stroke(q + 1))
        got = [x for x in out.getvalue().splitlines() if "udp sendto dropped" in x]
        assert len(got) == 2, out.getvalue()
        assert "OSError: " in got[0] and "ENOMEM" in got[0] and got[0].endswith(", 1 this boot"), got
        assert got[1].endswith(", %d this boot" % L.UDP_DROP_LOG_EVERY), got
        assert farm_ws.stats["udp_dropped"] == L.UDP_DROP_LOG_EVERY + 1


@case
def test_udp_send_says_so_when_sendto_takes_only_part_of_a_datagram():
    with rig("udp"):
        pool = Pool()
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        sock = farm_ws._udp_sock()
        sock.sendto = lambda data, dest: 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert not farm_ws.send(hb_doc(1), keep=False)
        assert "udp sendto dropped: sent 0 of " in out.getvalue(), out.getvalue()
        assert farm_ws.stats["udp_dropped"] == 1



# --- the UDP heartbeat: one datagram ----------------------------------------
LONG = "x" * 200


def worst_hb():
    """heartbeat_doc() as the Pico can make it at its biggest: body.net =
    the real farm_ws.link_status() (every counter, 5-6 digits), all 8
    chans faulted with an 80-char why, long strings, floats everywhere."""
    for k, v in farm_ws.stats.items():
        if isinstance(v, int) and not isinstance(v, bool):
            farm_ws.stats[k] = 123456
    farm_ws.stats["last_drop_reason"] = "send timeout " + LONG
    farm_ws.stats["last_reply_s"], farm_ws.stats["last_reply_b"] = 1.234567, 7012
    net = farm_ws.link_status()
    net.update({"wifi_up": True, "wifi_lost": 12345, "rejoins": 12345, "radio_resets": 12345,
                "gw_fail": 12345, "gw_ms": 1234.5678})
    stamp = {"tick_ms": 536870911, "utc_ms": 1793800000000, "err_ms": 12.345678,
             "sync": "synced", "acc": "ok", "epoch": 123456, "session": 123456,
             "mode": "ringing", "srv": "192.168.100.100", "stratum": 15}
    body = {"panel": [[255, 255, 255]] * 6,
            "panel_state": ["heartbeat_holdover"] * 6,
            "link": "wifi_only", "tower": "ringing_down", "net": net,
            "wifi": {"ip": "192.168.100.100", "rssi": -100},
            "sync": "unsynced",
            "time": {"srv": "192.168.100.100", "stratum": 15,
                     "last": {"ok": True, "server": "192.168.100.100", "stratum": 15,
                              "rtt_ms": 123.456789, "offset_ms": -12345.678901,
                              "drift_ppm": 123.45678901, "why": "rtt 123.4 > 56.7 " + LONG},
                     "peer": {"srv": "192.168.100.100", "stratum": 16, "leap": 3}},
            "last_seq": 123456789, "uid": "7091CDAB9DBD5305",
            "chan": [["unknown", "w" * 80]] * 8,
            "uptime_s": 123456789, "last_reset": "BROWNOUT " + LONG, "watchdog": True,
            "edges_lost": 123456, "late_drains": 123456, "strokes_undelivered": 123456,
            "mem_free": 123456789, "gc_heap_kb": 123456, "gc_heap_max_kb": 123456,
            "gc_heap_boot_kb": 123456, "gc_heap_grew": 123456}
    doc = dict(stamp, schema="sand.panel/v1", type="heartbeat", src="sandswing",
               id="sandswing-pico", boot_id="7091cdab", seq=123456789, body=body)
    return doc


def plain_dumps(obj):
    return json.dumps(obj)                 # CircuitPython without separators: ", " ": "


@case
def test_worst_case_heartbeat_is_too_big_whole_but_the_slim_one_fits_one_datagram():
    with rig("udp"):
        doc = worst_hb()
        whole = len(json.dumps(doc).encode("utf-8"))
        assert whole > 2 * L.UDP_MAX_B, whole                       # why every one was too_big
        assert len(json.dumps(doc["body"]["net"])) > L.UDP_MAX_B // 2  # body.net dominates
        for dumps in (L.dumps_compact, plain_dumps):
            text, dropped = L.udp_heartbeat(doc, dumps=dumps)
            assert len(text.encode("utf-8")) <= L.UDP_MAX_B and dropped == [], (dumps, len(text))
        assert len(L.udp_heartbeat(doc)[0]) < len(L.udp_heartbeat(doc, dumps=plain_dumps)[0])


@case
def test_slim_heartbeat_keeps_what_sandbells_reads_and_cuts_long_strings():
    with rig("udp"):
        doc = worst_hb()
        before = json.dumps(doc)
        slim = json.loads(L.udp_heartbeat(doc)[0])
        assert json.dumps(doc) == before                             # the /ws doc is not touched
        for k in ("schema", "type", "src", "id", "boot_id", "seq"):
            assert slim[k] == doc[k], k
        assert (slim["schema"], slim["type"]) == ("sand.panel/v1", "heartbeat")
        b, d = slim["body"], doc["body"]
        for k in ("panel", "panel_state", "sync", "uid", "link", "tower", "last_seq"):
            assert b[k] == d[k], k
        assert [c[0] for c in b["chan"]] == [c[0] for c in d["chan"]] and len(b["chan"]) == 8
        assert all(len(c[1]) == L.HB_UDP_STR_MAX for c in b["chan"])
        assert b["wifi"] == {"ip": "192.168.100.100", "rssi": -100}
        assert b["time"]["peer"] == {"srv": "192.168.100.100", "stratum": 16, "leap": 3}
        assert b["time"]["srv"] == "192.168.100.100"
        assert set(b["time"]["last"]) == {"ok", "why"} and len(b["time"]["last"]["why"]) == L.HB_UDP_STR_MAX
        assert b["net"]["drops"] == 123456 and len(b["net"]["last_drop_reason"]) == L.HB_UDP_STR_MAX
        assert set(b["net"]) == {"drops", "last_drop_reason"}     # the rest: console "udp stats"
        assert not set(slim) - set(L.HB_UDP_ENVELOPE) - {"body"}
        assert len(b["last_reset"]) == L.HB_UDP_STR_MAX
        if TEST_OUT:
            with open(os.path.join(TEST_OUT, "slim_hb.json"), "w") as f:
                f.write(L.udp_heartbeat(doc)[0])


@case
def test_too_big_slim_heartbeat_drops_optional_fields_in_order_and_still_goes():
    with rig("udp"):
        doc = worst_hb()
        full = len(L.udp_heartbeat(doc)[0].encode("utf-8"))
        text, dropped = L.udp_heartbeat(doc, max_b=full - 30)
        assert len(text) <= full - 30 and dropped and dropped[0] == "body.last_reset", dropped
        assert dropped == [".".join(p) for p in L.HB_UDP_DROP[:len(dropped)]]
        slim = json.loads(text)
        assert slim["body"]["panel"] and slim["body"]["uid"] and "last_reset" not in slim["body"]
        text, dropped = L.udp_heartbeat(doc, max_b=1)                # nothing fits: the core is left
        core = json.loads(text)
        assert "body.chan.why" in dropped and all(c[1] is None for c in core["body"]["chan"])
        assert set(core) == {"schema", "type", "src", "id", "boot_id", "seq", "body"}
        assert {"panel", "panel_state", "sync", "uid", "chan", "net", "wifi", "time"} == set(core["body"])
        assert core["body"]["net"] == {"drops": 123456} and core["body"]["wifi"] == {"rssi": -100}


@case
def test_udp_heartbeat_goes_as_one_slim_datagram_and_ws_gets_the_whole_doc():
    with rig("udp"):
        pool = Pool()
        farm_ws.configure_udp({"link": {"udp_host": "10.42.0.50"}}, pool)
        doc = worst_hb()
        for k in ("udp_too_big", "udp_sent", "udp_hb_trimmed"):
            farm_ws.stats[k] = 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(12):                                     # one minute of heartbeats
                assert farm_ws.send(doc, keep=False, udp_fit=L.udp_heartbeat)
        assert farm_ws.stats["udp_too_big"] == 0 and farm_ws.stats["udp_sent"] == 12
        assert farm_ws.stats["udp_hb_trimmed"] == 0 and "trimmed" not in out.getvalue()
        assert farm_ws.stats["udp_last_b"] == len(pool.sent[-1][0]) <= L.UDP_MAX_B
        assert json.loads(pool.sent[-1][0])["body"]["uid"] == "7091CDAB9DBD5305"
        assert farm_ws._event_boot[0] == "7091cdab"
        # without udp_fit (an event, or a caller that does not ask) it is as before
        assert not farm_ws.send(doc, keep=False) and farm_ws.stats["udp_too_big"] == 1
        # trimmed: said once, counted
        with contextlib.redirect_stdout(out):
            farm_ws.send(doc, keep=False, udp_fit=lambda o: L.udp_heartbeat(o, max_b=900))
        assert farm_ws.stats["udp_hb_trimmed"] == 1 and "udp heartbeat trimmed to" in out.getvalue()
    with rig("live") as r:                                          # /ws: the whole doc, as before
        doc = {"schema": "sand.panel/v1", "type": "heartbeat", "boot_id": "b",
               "body": {"net": {"connects": 1}, "pad": "y" * 100}}
        before = len(r.conn.data)
        farm_ws.send(doc, keep=False, udp_fit=L.udp_heartbeat)
        got = [json.loads(f) for f in frames(bytes(r.conn.data[before:]))]
        assert got == [doc], got


@case
def test_hb_diag_reports_the_datagram_size_when_given_one():
    d, lines = L.HeartbeatDiag(0.0), []
    d.gate(5.0, 0, True, lines.append)
    d.heartbeat(5.0, lambda: hb_doc(1), lambda doc: True, lines.append, size=lambda doc: 812)
    assert lines == ["5.000 hb #1: 812 B, ws=0 udp=1, sent=True"], lines



# --- back chat: Sandbells' acks (udp) ---------------------------------------
class AckPool(CmdPool):
    """The send socket (never bound) reads pool.acks; the command socket
    (bound to cmd_port) reads pool.inbox."""
    def __init__(self):
        CmdPool.__init__(self)
        self.acks, self.reads = [], 0

    def socket(self, fam, typ):
        s = CmdPool.socket(self, fam, typ)
        pool, cmd_read = self, s.recvfrom_into
        state = {"bound": False}
        bind = s.bind

        def bind2(addr):
            state["bound"] = True
            bind(addr)

        def recvfrom_into(buf):
            if state["bound"]:
                return cmd_read(buf)
            pool.reads += 1
            if not pool.acks:
                raise OSError(farm_ws.EAGAIN, "EAGAIN")
            item = pool.acks.pop(0)
            if isinstance(item, Exception):
                raise item
            data, peer = item
            n = min(len(buf), len(data))
            buf[:n] = data[:n]
            return len(data), peer
        s.bind, s.recvfrom_into = bind2, recvfrom_into
        return s


def ack(seq, boot_id="7091cdab", **kw):
    d = {"schema": L.ACK_SCHEMA, "boot_id": boot_id, "seq": seq}
    d.update(kw)
    return json.dumps(d, separators=(",", ":")).encode()


class ack_rig(udp_rig):
    def __enter__(self):
        rig.__enter__(self)
        self.pool = AckPool()
        farm_ws.configure_udp({"link": {"udp_host": SB}}, self.pool)
        self.real_uid = farm_ws._device_uid
        farm_ws._device_uid = lambda: UID
        farm_ws.send(stroke(1))                  # this boot's boot_id; the send socket exists
        return self


@case
def test_an_ack_from_udp_host_for_this_boot_turns_the_link_led_green_then_it_fades():
    with ack_rig("udp") as r:
        assert not farm_ws.sandbells_connected()             # udp, no ack yet: blue as before
        r.pool.acks.append((ack(4), (SB, L.UDP_PORT)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            farm_ws.poll()
        assert "udp ack: first from %s (hb seq 4)" % SB in out.getvalue(), out.getvalue()
        assert farm_ws.sandbells_connected() and farm_ws.ack_fresh()
        st = farm_ws.link_status()["ack"]
        assert st["acks"] == 1 and st["refused"] == 0 and st["ack_age_s"] == 0.0
        r.clock.t += L.ACK_OK_S - 0.1
        assert farm_ws.sandbells_connected()
        r.clock.t += 0.2
        assert not farm_ws.sandbells_connected()             # stale: blue again
        r.pool.acks.append((ack(5), (SB, L.UDP_PORT)))
        with contextlib.redirect_stdout(out):
            farm_ws.poll()
        assert farm_ws.sandbells_connected() and out.getvalue().count("first from") == 1


@case
def test_acks_from_anyone_else_another_boot_or_garbage_are_read_and_dropped_bounded():
    with ack_rig("udp") as r:
        bad = [(ack(1), ("10.9.9.9", 1)), (ack(1, boot_id="beefbeef"), (SB, 1)),
               (ack(1, schema="sand.cmd/v1"), (SB, 1)), (ack("1"), (SB, 1)),
               (b"not json", (SB, 1)), (b"x" * (L.ACK_MAX_B + 10), (SB, 1))]
        r.pool.acks.extend(bad)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r.pool.reads = 0
            farm_ws.ack_poll()
            assert r.pool.reads == L.ACK_POLL_N                 # bounded per poll
            farm_ws.ack_poll()
        assert not r.pool.acks and farm_ws._acks.refused == len(bad)
        assert out.getvalue().count("udp ack refused") == 1, out.getvalue()   # the first, then every 100th
        assert "not link.udp_host" in out.getvalue()
        assert not farm_ws.sandbells_connected() and farm_ws._acks.acks == 0


@case
def test_ack_poll_never_waits_and_a_socket_error_means_a_fresh_socket():
    with ack_rig("udp") as r:
        r.pool.reads = 0
        assert farm_ws.ack_poll() == 0 and r.pool.reads == 1  # EAGAIN: stop at once
        r.pool.acks.append(OSError(104, "ECONNRESET"))
        n = r.pool.socks
        assert farm_ws.ack_poll() == 0 and farm_ws._udp["sock"] is None
        assert farm_ws.send(stroke(2)) and r.pool.socks == n + 1   # the next send makes a new one
        farm_ws.configure_ring({"link": {"ring_send": "live"}})
        r.pool.acks.append((ack(1), (SB, 1)))
        assert farm_ws.ack_poll() == 0 and r.pool.acks       # not udp: not read at all


@case
def test_dead_link_is_no_ack_for_ack_dead_s_then_backs_off_60_300_900_capped():
    w = L.AckWatch()
    assert not w.due(0.0) and w.silent_s(0.0) is None        # nothing sent yet: no clock
    w.sent(100.0)
    w.sent(150.0)                                            # the clock starts at the first send
    assert not w.due(100.0 + L.ACK_DEAD_S - 1) and w.due(100.0 + L.ACK_DEAD_S)
    t = 100.0 + L.ACK_DEAD_S
    gaps = []
    for _ in range(5):
        n = w.tried(t)
        gap = w.wait_s()
        assert not w.due(t + gap - 1) and w.due(t + gap)
        gaps.append(gap)
        t += gap
    assert gaps == [60.0, 300.0, 900.0, 900.0, 900.0] and n == 5, gaps
    w.ack(t, 9)                                              # Sandbells is back: from the start
    assert w.tries == 0 and w.wait_s() == L.ACK_DEAD_S and w.fresh(t)
    assert not w.due(t + L.ACK_DEAD_S - 1) and w.due(t + L.ACK_DEAD_S)
    w.restart()                                              # Wi-Fi came back: next send restarts it
    assert not w.due(t + 1000) and w.silent_s(t) is None


@case
def test_ack_settings_are_optional_defaults_in_code_and_0_never_rejoins():
    w = L.AckWatch()
    w.configure(None)
    assert (w.ok_s, w.dead_s) == (L.ACK_OK_S, L.ACK_DEAD_S)
    w.configure({"ack_ok_s": "x", "ack_dead_s": None})
    assert (w.ok_s, w.dead_s) == (L.ACK_OK_S, L.ACK_DEAD_S)
    w.configure({"ack_ok_s": 20, "ack_dead_s": 0})
    w.sent(0.0)
    assert w.ok_s == 20.0 and not w.due(1e6)
    with ack_rig("udp"):
        farm_ws.configure_udp({"link": {"udp_host": SB, "ack_dead_s": 90}})
        assert farm_ws._acks.dead_s == 90.0
        farm_ws.configure_udp({"link": {"udp_host": SB}})
        assert farm_ws._acks.dead_s == L.ACK_DEAD_S


@case
def test_ack_dead_due_only_in_udp_and_counts_from_the_first_send():
    with ack_rig("udp") as r:
        t0 = r.clock.t
        assert not farm_ws.ack_dead_due(t0 + L.ACK_DEAD_S - 1)
        assert farm_ws.ack_dead_due(t0 + L.ACK_DEAD_S)
        r.pool.acks.append((ack(1), (SB, 1)))
        farm_ws.ack_poll(t0 + 30)
        assert not farm_ws.ack_dead_due(t0 + L.ACK_DEAD_S) and farm_ws.ack_dead_due(t0 + 30 + L.ACK_DEAD_S)
        farm_ws.configure_ring({"link": {"ring_send": "live"}})
        assert not farm_ws.ack_dead_due(t0 + 1e6)


@case
def test_ack_check_is_a_pure_table():
    ok = {"schema": L.ACK_SCHEMA, "boot_id": "B", "seq": 3}
    assert L.ack_check(ok, "h", "h", "B") == (True, None)
    assert not L.ack_check(ok, "h", None, "B")[0]
    assert not L.ack_check(ok, "h", "h", None)[0]
    assert not L.ack_check(dict(ok, seq=True), "h", "h", "B")[0]
    assert not L.ack_check([], "h", "h", "B")[0]


finish()
