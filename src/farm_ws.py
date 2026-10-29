# farm_ws.py — HTTP /status + up to MAX_WS_CLIENTS /ws clients (Sand* farm)
#
# Shared by sandswing, sandsense, and other timed Sand* devices.
# Pattern for /ws accept taken from sandsense (Websocket(request)).
# Every client gets every send (sandstore, sandbells, debug listeners).
# Over the limit the NEW client is refused (accepted, then closed with
# 1013 "try again later"): long-lived clients such as sandstore are never
# kicked off by an ad-hoc listener.
#
# Link health (see docs/link-health.md):
#   * A client that sends any text frame (Sandbells ingest and page 3 send a
#     small heartbeat every few seconds) is "live".  Once a client has sent
#     one, it must keep sending: silent for CLIENT_SILENT_S -> dropped.  A
#     client that never sends (old ingest, a debug listener) is never timed
#     out, as before.
#   * Sends are bounded (SEND_TIMEOUT_S): the library's send loops forever
#     on EAGAIN when the TCP buffer is full (dead peer), which froze the
#     whole concentrator.  A send that cannot finish drops that client.
#   * The last REPLAY_N events are kept and replayed to every new client, so
#     strokes made while the link was down still arrive (Sandbells ignores
#     re-sends: UNIQUE(src, boot_id, seq)).
#   * What a new client gets (accepted or refused with a 503, how many events,
#     how fast, with what send budget) is decided by link_health.SendPath, not
#     here: see its SEND_LEVELS / SEND_TRANSITIONS table.  This file reports
#     every send's duration to it and does what the plan says.
#   * A poll() is bounded (link_health.POLL_BUDGET_S): the replay is sent a
#     frame or two per poll turn and resumes next turn, so one slow send
#     cannot hold the sense loop for the rest of a replay.
#   * Each client has its own small machine, link_health.ClientPath (NEW,
#     LIVE, LAGGING, GONE; CLIENT_TRANSITIONS).  This file reports what
#     happened to a client (heard, quiet, silent, a send that missed its
#     deadline, lwIP starved, a newer connection from the same peer, an
#     error, a close) through _event() and does the action the table gives:
#     hold, resume, close, or cut (drop without a close frame).  No other
#     place decides to drop a client.
#   * Every connect / drop / refusal is counted, kept as last_drop_reason,
#     and queued as a link note: concentrator.py turns notes into "link"
#     events (take_link_notes()).
import json
import time
from lastline import print        # print as before, and remember the line
import wifi
import adafruit_connection_manager
import link_health                 # the send-path state model: this file obeys it
try:
    import ws_bounds               # deadlines on the library's waits, and what they cost
except ImportError:                # pragma: no cover
    ws_bounds = None

try:
    from adafruit_httpserver import Server, Request, Response, GET, Websocket
    HAS_HTTP = True
except ImportError:
    HAS_HTTP = False

MAX_WS_CLIENTS = 4             # RAM: one socket + small buffers each
WS_NORMAL = 1000               # close codes (RFC 6455)
WS_GOING_AWAY = 1001
WS_TRY_AGAIN_LATER = 1013

try:
    from errno import EAGAIN, ETIMEDOUT   # the Pico's newlib: ETIMEDOUT is 116, not 110
except ImportError:                     # pragma: no cover
    EAGAIN = 11
    ETIMEDOUT = 110

CLIENT_SILENT_S = link_health.CLIENT_SILENT_S  # a heartbeat client silent this long is dead
CLIENT_LAG_S = link_health.CLIENT_LAG_S        # ...this long: stop writing to it (LAGGING)
STALL_S = 5.0                  # a poll pass this slow counts as a stall (link health)
_stalls = []                    # monotonic times of recent stalls, newest last
_answered = [0]                 # clients cut for a stall (overran / starved): see poll()
_turn = [0]                     # poll() count: which client is read first
SLOW_POLL_S = 2.0              # a poll pass slower than this is logged
SEND_TIMEOUT_S = link_health.SEND_TIMEOUT_S   # most one send may block on a full TCP buffer
RX_FRAMES_PER_POLL = 4         # frames read per client per poll()
REPLAY_N = link_health.SEND_LEVELS[0][1]      # recent events kept for replay (level 0 size)
TODO_MAX = 2 * REPLAY_N        # events waiting behind a replay in progress, per client
NOTES_MAX = 16                 # link notes waiting for concentrator.py

_server = None
_last_route = [None, 0.0]       # name and duration of the slowest-looking handler
_host = None                   # IP the server is bound to
_cfg = None
_clients = []                  # live Websocket objects, oldest first
_refused = []                  # accepted over the limit; closed in poll()
_new = []                      # accepted this poll: replay + note in poll()
_meta = {}                     # id(ws) -> {"at", "rx", "hb"}
_recent = []                   # last REPLAY_N event messages (json str)
_notes = []                    # (what, info) for concentrator.py
_replies = []                  # queued HTTP replies (/status), oldest first: see _pump_replies
_path = link_health.SendPath() # send-path state model: plan(now) says what is allowed
_gate = link_health.StatusGate() # /status full or held, from the tower state: set_tower()
_payload_fn = lambda: {}
stats = {"connects": 0, "drops": 0, "refused": 0, "send_timeouts": 0,
         "silent_drops": 0, "refused_backoff": 0, "replays_done": 0,
         "cuts": 0, "lagging": 0, "superseded": 0, "starved": 0,
         "stalls_answered": 0, "replies": 0, "replies_done": 0,
         "replies_cancelled": 0, "replies_held": 0,
         "last_reply_s": None, "last_reply_b": None,
         "last_drop_reason": None, "last_drop_at": None,
         "hb_seen": False, "last_rx_at": None}


def wifi_candidates(cfg):
    nets = (cfg.raw.get("wifi") or {}).get("networks") or []
    if nets:
        return sorted(nets, key=lambda n: n.get("priority", 99))
    ssid = cfg.get_str("wifi.ssid", "")
    pw = cfg.get_str("wifi.password", "")
    return [{"ssid": ssid, "password": pw}] if ssid else []


_joined_ssid = None   # remembered because ap_info is not available on every build


def apply_static_ip(radio, ip):
    """Keep our usual address on a network whose DHCP will not give it (an
    extender hides our MAC from the router's reservation: 2026-10-26 the
    Pico got .253 instead of .81).  Netmask, gateway and DNS stay as the
    DHCP lease set them.  Always set the address when one is configured,
    even if DHCP happens to hand out the same one: setting it is what stops
    the DHCP client, and 2026-10-28 13:52 a lease that agreed at the join
    moved to .253 two seconds later.  Any failure leaves the DHCP address in
    place.  Returns the address now in use."""
    if not ip:
        return radio.ipv4_address
    try:
        import ipaddress
        want = ipaddress.ip_address(ip)
        was = str(radio.ipv4_address)
        radio.set_ipv4_address(ipv4=want, netmask=radio.ipv4_subnet,
                               gateway=radio.ipv4_gateway,
                               ipv4_dns=getattr(radio, "ipv4_dns", None))
        print("static IP", ip, "(DHCP gave %s)" % was)
    except Exception as e:
        print("static IP failed:", e)
    return radio.ipv4_address


def connect_wifi(cfg, timeout=None):
    global _joined_ssid
    last = None
    for n in wifi_candidates(cfg):
        ssid, password = n.get("ssid", ""), n.get("password", "")
        if not ssid:
            continue
        try:
            print("Trying WiFi:", ssid)
            if timeout is None:
                wifi.radio.connect(ssid, password)
            else:
                wifi.radio.connect(ssid, password, timeout=timeout)
            apply_static_ip(wifi.radio, n.get("static_ip"))
            print("WiFi OK", ssid, wifi.radio.ipv4_address)
            _joined_ssid = ssid
            return True
        except Exception as e:
            last = e
            print("Fail:", ssid, e)
    print("WiFi failed:", last)
    return False


def start_server(cfg, payload_fn):
    """payload_fn() -> dict for GET /status. Also accepts /ws clients
    (up to MAX_WS_CLIENTS)."""
    global _server, _payload_fn, _host, _cfg
    _payload_fn = payload_fn
    _cfg = cfg
    if not HAS_HTTP:
        print("adafruit_httpserver missing – no /ws")
        return None
    if not cfg.get_bool("output.ws.enabled", False) and "ws" not in (
        cfg.raw.get("output") or {}
    ).get("enabled_formats", []):
        print("output.ws disabled")
        return None
    try:
        import ws_bounds
        import adafruit_httpserver.response as _resp
        print("ws bounds:", "on" if ws_bounds.install(_resp) else "NOT installed (library differs)")
        print("request bounds:", "on" if ws_bounds.install_server(Server) else "NOT installed (library differs)")
    except ImportError as e:
        print("ws bounds: not installed:", e)
    try:
        pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
        server = Server(pool, debug=False)
        # each recv on an accepted socket waits at most this (the library's
        # default is 1 s): a connection that never sends its request costs
        # REQ_BOUND_S, then the library closes it.  Also caps the wait for
        # send-buffer space on that socket (not lwIP's ERR_MEM sleep).
        server.socket_timeout = link_health.REQ_BOUND_S
        port = cfg.get_int("network.http_port", 80)
        path_status = cfg.get_str("network.status_path", "/status")
        path_ws = cfg.get_str("network.ws_path", "/ws")

        @server.route(path_status, GET)
        def status_route(request: Request):
            return route_status(request, path_status)

        @server.route(path_ws, GET)
        def ws_route(request: Request):
            return route_ws(request)

        host = str(wifi.radio.ipv4_address)
        server.start(host=host, port=port)
        print("HTTP http://%s%s" % (host, path_status))
        print("WS   ws://%s%s" % (host, path_ws))
        _server = server
        _host = host
        return server
    except Exception as e:
        print("WS server failed:", e)
        _server = None
        return None


def server_running():
    return _server is not None


def server_host():
    return _host


def stop_server():
    """Drop every client and stop listening (Wi-Fi gone, or a new IP)."""
    global _server, _host
    drop_all("server stop")
    if _server is not None:
        try:
            _server.stop()
        except Exception as e:
            print("ws stop:", e)
    _server = None
    _host = None


def wifi_ok():
    """Associated and holding an IPv4 address (CircuitPython wifi.radio)."""
    r = getattr(wifi, "radio", None)
    if r is None or getattr(r, "ipv4_address", None) is None:
        return False
    return bool(getattr(r, "connected", True))


def wifi_ip():
    r = getattr(wifi, "radio", None)
    ip = getattr(r, "ipv4_address", None) if r is not None else None
    return None if ip is None else str(ip)


def wifi_rssi():
    try:
        return wifi.radio.ap_info.rssi
    except Exception:
        return None


def wifi_ssid():
    """The network we are joined to (the config's ssid can be a list).
    ap_info is None on some builds, so fall back to what we joined."""
    try:
        ssid = wifi.radio.ap_info.ssid
        ssid = ssid.decode() if isinstance(ssid, (bytes, bytearray)) else ssid
        if ssid:
            return ssid
    except Exception:
        pass
    return _joined_ssid


last_ping_ms = None     # round trip of the last answered gateway ping


def gateway_ping(timeout=1.0):
    """True if the Wi-Fi gateway answers a ping, False if not, None if this
    build cannot ping or has no gateway.  Sets last_ping_ms (None on a miss)."""
    global last_ping_ms
    try:
        gw = wifi.radio.ipv4_gateway
        if gw is None:
            return None
        r = wifi.radio.ping(gw, timeout=timeout)
    except Exception:
        return None
    last_ping_ms = None if r is None else round(r * 1000, 1)
    return r is not None


def _count():
    return "(%d client%s)" % (len(_clients), "" if len(_clients) == 1 else "s")


def _note(what, **info):
    info["clients"] = len(_clients)
    _notes.append((what, info))
    del _notes[:-NOTES_MAX]


def take_link_notes():
    """-> [(what, info)] queued since the last call (oldest first)."""
    out = list(_notes)
    del _notes[:]
    return out


def _peer(ws):
    try:
        return str(ws._request.client_address[0])
    except Exception:
        return None


_queued_cls = []


def _queued_response_class():
    """A Response whose _send() collects the bytes the library would have
    written (headers, then body) and queues them as one reply job for
    _pump_replies, leaving the socket open.  Built once, from the library's
    Response, so the headers are exactly the library's."""
    if not _queued_cls:
        class QueuedResponse(Response):
            def _send_bytes(self, conn, buffer):
                self._out.append(bytes(buffer))

            def _close_connection(self):
                pass

            def _send(self):
                self._out = []
                body = self._body.encode("utf-8") if isinstance(self._body, str) else self._body
                self._send_headers(len(body), self._content_type)
                self._out.append(body)
                req = self._request
                try:
                    peer = str(req.client_address[0])
                except Exception:
                    peer = None
                data = b"".join(self._out)
                self._out = None
                self._size = len(data)
                _queue_reply(req.connection, data, peer)
        _queued_cls.append(QueuedResponse)
    return _queued_cls[0]


def route_status(request, path_status="/status"):
    """GET /status: the full document, or the held one while the tower
    rings (StatusGate); either way queued for the reply writer."""
    t_route = time.monotonic()
    if _gate.state == link_health.STATUS_HELD:
        # the tower is ringing: a few hundred bytes, no payload built
        stats["replies_held"] += 1
        _last_route[0], _last_route[1] = path_status, time.monotonic() - t_route
        return _queued_response_class()(
            request,
            body=held_body(),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*",
                     "Retry-After": str(link_health.STATUS_HELD_RETRY_S)},
        )
    try:
        body = json.dumps(_payload_fn())
    except MemoryError as e:             # fragmented heap: tidy, retry once
        print("status payload error", e)
        try:
            import gc
            gc.collect()
            body = json.dumps(_payload_fn())
        except Exception as e2:
            print("status payload retry failed", e2)
            body = '{"error":"payload"}'
    except Exception as e:
        print("status payload error", e)
        body = '{"error":"payload"}'
    _last_route[0], _last_route[1] = path_status, time.monotonic() - t_route
    # not written here: queued, and written a chunk per poll turn
    # (link_health REPLY_TRANSITIONS; one 5964 B send() stalled 10 s)
    return _queued_response_class()(
        request,
        body=body,
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def held_body():
    """The /status answer while the tower rings: a valid sand.status/v1
    document (renders as one Tower section even in an unpatched Sandbells)
    with held set, so a patched page keeps the last full status on screen."""
    tower = _gate.tower
    return json.dumps({
        "schema": "sand.status/v1", "family": "sandswing", "held": "ringing",
        "tower": tower, "retry_after_s": link_health.STATUS_HELD_RETRY_S,
        "level": "ok",
        "summary": "Ringing (%s): full status paused so strokes have the link" % tower,
        "sections": [{"id": "tower", "title": "Tower", "items": [
            {"key": "tower", "label": "Tower", "value": tower,
             "level": "ok" if tower == "active" else "warn"}]}]})


def set_tower(tower, now=None):
    """TowerWatch's tower state, every tower step; StatusGate acts on changes."""
    now = time.monotonic() if now is None else now
    action = _gate.fire(tower, now)
    if action == "hold":
        cut = [j for j in _replies]
        for job in cut:
            _reply_event(job, "held", "tower %s" % tower)
        _note("status_held", tower=tower, cancelled=len(cut))
    elif action == "resume":
        _note("status_resumed", tower=tower, held_replies=stats["replies_held"])
    return action


def _queue_reply(conn, data, peer=None, now=None):
    """Hand a finished reply to the single writer (or turn it away)."""
    now = time.monotonic() if now is None else now
    job = {"conn": conn, "data": data, "done": 0, "at": now, "peer": peer,
           "path": link_health.ReplyPath()}
    stats["replies"] += 1
    if len(_replies) >= link_health.REPLY_QUEUE_MAX:
        _reply_event(job, "overflow", "%d replies queued" % len(_replies))
        return job
    try:
        conn.setblocking(False)       # EAGAIN, not a wait, when lwIP has no room
    except Exception:
        pass
    _replies.append(job)
    return job


def _reply_event(job, event, why=None):
    """Tell a reply's ReplyPath what happened; do what REPLY_TRANSITIONS says."""
    action = job["path"].fire(event)
    if action in ("close", "abort"):
        if job in _replies:
            _replies.remove(job)
        try:
            job["conn"].close()
        except Exception:
            pass
        took = round(time.monotonic() - job["at"], 2)
        if action == "close":
            stats["replies_done"] += 1
            stats["last_reply_s"], stats["last_reply_b"] = took, len(job["data"])
        else:
            stats["replies_cancelled"] += 1
            _note("reply_cancelled", why=event if why is None else str(why),
                  sent_b=job["done"], size_b=len(job["data"]), took_s=took,
                  peer=job["peer"])
            print("%.3f /status reply cancelled (%s): %d of %d B in %.1f s"
                  % (time.monotonic(), event if why is None else why,
                     job["done"], len(job["data"]), took))
    return action


def _pump_replies(plan, deadline):
    """The single reply writer: deadlines for every queued reply, then at
    most REPLY_CHUNKS_PER_TURN chunks of REPLY_CHUNK_B for the oldest one,
    inside the poll budget.  Nothing is written while the send path drains."""
    now = time.monotonic()
    for job in list(_replies):
        if now - job["at"] > link_health.REPLY_DEADLINE_S:
            _reply_event(job, "deadline", "deadline %.1f s" % link_health.REPLY_DEADLINE_S)
    if not _replies or not plan.writes:
        return
    job = _replies[0]
    _reply_event(job, "turn")
    for _ in range(link_health.REPLY_CHUNKS_PER_TURN):
        if deadline - time.monotonic() < link_health.MIN_SLICE_S:
            return
        chunk = memoryview(job["data"])[job["done"]:job["done"] + link_health.REPLY_CHUNK_B]
        t0 = time.monotonic()
        try:
            n = job["conn"].send(chunk)
        except OSError as e:
            t1 = time.monotonic()
            if e.errno == EAGAIN:                   # no room in lwIP now: next turn
                _path.observe(t1, "reply", t1 - t0, True)
                _reply_event(job, "blocked")
                return
            _path.observe(t1, "reply", t1 - t0, False)
            _reply_event(job, "failed", _why(e))
            return
        t1 = time.monotonic()
        job["done"] += n or 0
        _path.observe(t1, "reply", t1 - t0, True)
        if t1 - t0 >= link_health.STARVED_S:
            # lwIP's ERR_MEM sleep, and we know which write it was: this
            # stall is the reply's, answered by cancelling it and draining
            _answered[0] += 1
            _reply_event(job, "starved", "one %d B chunk took %.1f s" % (len(chunk), t1 - t0))
            _starved("reply", t1 - t0)
            return
        if job["done"] >= len(job["data"]):
            _reply_event(job, "done")
            return
        if not n or n < len(chunk):
            _reply_event(job, "blocked")
            return
        _reply_event(job, "sent")


def _busy_response(request, retry_after_s):
    """A short HTTP 503 for a /ws request refused by the send-path model: no
    handshake, no socket kept, a few hundred bytes through the bounded reply
    path.  A browser or websocket-client sees a failed connect and backs off."""
    return Response(request, body="busy", status=(503, "Service Unavailable"),
                    headers={"Retry-After": str(int(retry_after_s + 0.999)),
                             "Connection": "close",
                             "Access-Control-Allow-Origin": "*"})


def route_ws(request):
    """GET /ws: ask the send-path model, obey the answer."""
    plan = _path.plan(time.monotonic())
    if not plan.accept:
        _path.note_refused()
        stats["refused_backoff"] += 1
        return _busy_response(request, plan.retry_after_s)
    return _accept(Websocket(request))


def _accept(ws):
    """Add a client, or queue it for a polite refusal when full.  The
    handshake must go out first, so the refusal close (and the replay of
    recent events to a new client) is sent in poll()."""
    if len(_clients) >= MAX_WS_CLIENTS:
        _refused.append(ws)
    else:
        peer = _peer(ws)
        _clients.append(ws)
        _meta[id(ws)] = {"at": time.monotonic(), "rx": None, "hb": False,
                         "todo": [], "left": 0, "next_at": 0.0,
                         "path": link_health.ClientPath(peer), "unheard_b": 0}
        _new.append(ws)
        stats["connects"] += 1
        print("%.3f WS client connected %s" % (time.monotonic(), _count()))
        # the display reconnects and leaves its old connection behind: tell
        # the older clients from this peer (the table cuts only a LAGGING one)
        if peer is not None:
            for old in [c for c in _clients if c is not ws and _peer(c) == peer]:
                _event(old, "superseded", "superseded by a new connection from %s" % peer)
    return ws


CLOSE_TIMEOUT_S = link_health.CLOSE_TIMEOUT_S   # a close frame to a stuck peer is not worth more


def _close(ws, code, frame=True):
    """Close politely if the socket takes the close frame within
    CLOSE_TIMEOUT_S, then shut it.  The library's close() sends through the
    same EAGAIN-forever loop as send_message(), so it is only used when the
    internals are missing (tests' fakes).  frame=False (the model's CUT): no
    close frame, the peer is not reading it and it would only queue in lwIP."""
    conn = getattr(getattr(ws, "_request", None), "connection", None)
    if conn is None or getattr(ws, "_prepare_frame", None) is None:
        try:
            ws.close(code)
        except Exception:
            pass
        return
    if frame and not getattr(ws, "closed", False):
        t0 = time.monotonic()
        ok = False
        try:
            payload = b"" if code is None else int(code).to_bytes(2, "big")
            _send_frame(ws, 8, payload, CLOSE_TIMEOUT_S)     # 8 = CLOSE
            ok = True
        except Exception:
            pass
        t1 = time.monotonic()
        _path.observe(t1, "close", t1 - t0, ok)
    try:
        conn.close()
    except Exception:
        pass
    ws.closed = True


_ERRNO_WORDS = {32: "broken pipe", 104: "connection reset", 107: "not connected",
                110: "timed out", 113: "no route to host", 116: "timed out",
                128: "not connected"}


def _why(why):
    """Readable drop reason: CircuitPython's OSError(107) prints just '107'."""
    if isinstance(why, OSError) and why.args and isinstance(why.args[0], int):
        word = why.args[1] if len(why.args) > 1 else _ERRNO_WORDS.get(why.args[0])
        if word:
            return "%s (errno %d)" % (word, why.args[0])
    return str(why) or type(why).__name__


def _drop(ws, why, code=WS_GOING_AWAY, frame=True, how=None):
    """Forget one client; close its socket quietly; count and note it.
    Called for a client by _event() (the ClientPath table decides) and by
    drop_all() (Wi-Fi lost, server stop)."""
    known = ws in _clients
    if known:
        _clients.remove(ws)
    m = _meta.pop(id(ws), None)
    if ws in _new:
        _new.remove(ws)
    _close(ws, code, frame)
    if isinstance(why, Exception) and not isinstance(why, OSError):
        # a bug, not a network fault: put the traceback in the serial log
        try:
            import sys
            sys.print_exception(why)
        except Exception:
            print("trace:", repr(why))
    why = _why(why)
    if known:
        stats["drops"] += 1
        stats["last_drop_reason"] = why
        stats["last_drop_at"] = time.monotonic()
        info = {"reason": why, "hb": bool(m and m["hb"]),
                "up_s": None if m is None else round(time.monotonic() - m["at"], 1)}
        if how is not None:                  # the ClientPath decided: say how, and
            info["how"] = how                # what was written since it was last heard
            info["peer"] = None if m is None else m["path"].peer
            info["src"] = None if m is None else m.get("src")
            info["unheard_b"] = None if m is None else m["unheard_b"]
        _note("client_dropped", **info)
    print("%.3f WS client dropped: %s %s" % (time.monotonic(), why, _count()))


def drop_all(why):
    for ws in list(_clients):
        _drop(ws, why)


def _event(ws, event, why=None):
    """Tell ws's ClientPath what happened; do what CLIENT_TRANSITIONS says.
    Returns the action (None: nothing changes)."""
    m = _meta.get(id(ws))
    if m is None or ws not in _clients:
        return None
    path = m["path"]
    before = path.state
    action = path.fire(event)
    if action in (link_health.CLOSE, link_health.CUT):
        if action == link_health.CUT:
            stats["cuts"] += 1
        if event in ("overran", "starved"):
            _answered[0] += 1
        if event == "silent":
            stats["silent_drops"] += 1
        elif event == "superseded":
            stats["superseded"] += 1
        _drop(ws, event if why is None else why,
              WS_NORMAL if event == "closed" else WS_GOING_AWAY,
              frame=(action == link_health.CLOSE
                     and link_health.SEND_WRITES[_path.state]),
              how="%s %s->%s" % (action, before, event))
    elif action == link_health.HOLD:
        stats["lagging"] += 1
        _note("client_lagging", peer=path.peer, src=m.get("src"), unheard_b=m["unheard_b"],
              quiet_s=None if m["rx"] is None else round(time.monotonic() - m["rx"], 1))
        print("%.3f WS client lagging (%s): no frame for %d s, holding sends %s"
              % (time.monotonic(), path.peer, CLIENT_LAG_S, _count()))
        if _newer_from_same_peer(ws):
            _event(ws, "superseded", "superseded by a newer connection from %s" % path.peer)
    elif action == link_health.RESUME:
        m["next_at"] = 0.0                       # what queued goes out on the next turn
        _note("client_resumed", peer=path.peer, queued=len(m["todo"]))
    return action


def _newer_from_same_peer(ws):
    peer = _peer(ws)
    if peer is None or ws not in _clients:
        return False
    return any(_peer(c) == peer for c in _clients[_clients.index(ws) + 1:])


def _starved(where, took):
    """A single send/serve call took STARVED_S or more: lwIP was out of
    buffers (the C ERR_MEM sleep).  Every client hears it; the table cuts
    the ones that have stopped talking (LAGGING), the likely cause."""
    if took < link_health.STARVED_S:
        return
    stats["starved"] += 1
    for ws in list(_clients):
        _event(ws, "starved", "not reading while lwIP starved (%s took %.1f s)"
               % (where, took))


def _deliver(ws, msg, timeout_s, kind):
    """One frame to one client, its outcome reported to the client's model:
    a send that fails, misses its deadline (also when it completed late:
    the C sleep in lwip_tcp_send cannot be interrupted), or runs slow while
    replaying is an event.  True if the frame went out in time."""
    t0 = time.monotonic()
    try:
        bad = _send_text(ws, msg, timeout_s, kind)
    except OSError as e:
        took = time.monotonic() - t0
        _event(ws, "overran" if e.errno in (ETIMEDOUT, EAGAIN) else "failed",
               "not reading: %s" % _why(e) if e.errno in (ETIMEDOUT, EAGAIN) else e)
        _starved("%s send" % kind, took)
        return False
    except Exception as e:
        _event(ws, "failed", e)
        return False
    took = time.monotonic() - t0
    if took > timeout_s:
        stats["send_timeouts"] += 1
        _event(ws, "overran", "%s frame took %.2f s (deadline %.2f s)" % (kind, took, timeout_s))
        _starved("%s send" % kind, took)
        return False
    if bad and kind == "replay":
        _event(ws, "overran", "replay slow (%.2f s)" % took)
        return False
    m = _meta.get(id(ws))
    if m is not None:
        m["unheard_b"] += len(msg)
    return True


def _send_frame(ws, opcode, payload, timeout_s):
    """One frame through ws's socket, giving up after timeout_s."""
    frame = ws._prepare_frame(opcode, payload)
    conn = ws._request.connection
    view = memoryview(frame)
    done, total = 0, len(frame)
    deadline = time.monotonic() + timeout_s
    while done < total:
        try:
            n = conn.send(view[done:])
        except OSError as e:
            if e.errno != EAGAIN:
                raise
            n = 0
        if n:
            done += n
        if done < total and time.monotonic() > deadline:
            # also when bytes are trickling through: a barely-alive link that
            # accepts a few bytes at a time held the Pico for 55 s
            raise OSError(ETIMEDOUT, "send timeout %.1f s" % timeout_s)


def _send_text(ws, msg, timeout_s=None, kind="live"):
    """One text frame with a deadline.  adafruit_httpserver 4.8.2
    send_message() retries EAGAIN forever (Response._send_bytes), so a dead
    peer with a full TCP buffer hangs everything; this gives up after
    timeout_s (SEND_TIMEOUT_S) and raises, so the caller drops the client.
    Falls back to send_message() if the library internals are not there.
    How long it took is reported to the send-path model (kind: "replay" or
    "live"); True is returned when the model counts the send as bad (slow)."""
    timeout_s = SEND_TIMEOUT_S if timeout_s is None else timeout_s
    t0 = time.monotonic()
    ok = False
    try:
        prep = getattr(ws, "_prepare_frame", None)
        conn = getattr(getattr(ws, "_request", None), "connection", None)
        if prep is None or conn is None:
            ws.send_message(msg, fail_silently=True)
        else:
            try:
                _send_frame(ws, 1, msg.encode("utf-8"), timeout_s)   # 1 = TEXT
            except OSError as e:
                if e.errno == ETIMEDOUT:
                    stats["send_timeouts"] += 1
                raise
        ok = True
    finally:
        t1 = time.monotonic()
        bad = _path.observe(t1, kind, t1 - t0, ok)
    return bad


def _receive(ws, now):
    """Read up to RX_FRAMES_PER_POLL frames; any text frame marks the client
    live (and a heartbeat client from then on)."""
    for _ in range(RX_FRAMES_PER_POLL):
        msg = ws.receive(fail_silently=True)   # handles ping/pong/close
        if ws.closed or msg is None:
            return
        stats["last_rx_at"] = now
        m = _meta.get(id(ws))
        if m is not None:
            m["rx"] = now
            m["unheard_b"] = 0
            if m.get("src") is None and isinstance(msg, str) and '"src"' in msg:
                try:                              # who it is: sandbells-page, the ingest...
                    m["src"] = str(json.loads(msg).get("src"))[:24]
                except Exception:
                    m["src"] = "?"
            if not m["hb"]:
                m["hb"] = True
                stats["hb_seen"] = True
            _event(ws, "heard")


def _flush_path_events():
    for what, info in _path.take_events():
        _note(what, **info)           # concentrator.py prints it as a `link ...` line


def _begin_replay(ws, plan):
    """A new client: queue the newest plan.replay_n events (none at the cap)."""
    m = _meta.get(id(ws))
    if m is None:
        return
    todo = list(_recent)[-plan.replay_n:] if plan.replay_n > 0 else []
    m["todo"], m["left"], m["next_at"] = todo, len(todo), 0.0
    _note("client_connected", peer=_peer(ws), replay=len(todo),
          level=plan.level)


def _pump(ws, m, plan, now, deadline):
    """Send this client's next chunk: plan.chunk_frames replay frames, then,
    once the replay part is out, whatever live events queued behind it, all
    inside the poll budget.  A send that misses its deadline (or a slow
    replay frame) is an event for the client's model, which cuts it; the
    send-path model has been told too and decides the backoff."""
    sent = 0
    while m["todo"]:
        left = deadline - time.monotonic() - plan.close_s    # keep room to drop it
        if left < link_health.MIN_SLICE_S:
            return                                  # resume next turn
        replaying = m["left"] > 0
        if replaying and sent >= plan.chunk_frames:
            break
        if not _deliver(ws, m["todo"][0], min(plan.send_s, left),
                        "replay" if replaying else "live"):
            return                                  # the model cut it (or it failed)
        m["todo"].pop(0)
        sent += 1
        if replaying:
            m["left"] -= 1
            if m["left"] == 0:
                stats["replays_done"] += 1
    m["next_at"] = now + plan.chunk_gap_s


def poll():
    """Accept HTTP/WS requests, refuse clients over the limit, replay recent
    events to new clients (a chunk per turn), then read each client so pings
    get a pong, a close (or a dead socket) is noticed, and a silent heartbeat
    client is dropped.  A bad client is dropped on its own.  Every phase
    stops when link_health.POLL_BUDGET_S is spent and carries on next turn."""
    t0 = time.monotonic()
    answered0 = _answered[0]
    plan = _path.plan(t0)
    deadline = t0 + plan.poll_s
    if ws_bounds is not None:
        ws_bounds.clear_last()
    if _server and plan.serve:          # SEND_DRAIN: not even a 503 (it is a send too)
        try:
            _server.poll()
        except Exception as e:
            print("ws poll:", e)
        t_srv = time.monotonic()
        _path.observe(t_srv, "server", t_srv - t0, True)
        if ws_bounds is not None and ws_bounds.last["reply_s"] >= link_health.STARVED_S:
            _answered[0] += 1         # the library's own reply (handshake, 503): cause known
        _starved("server", t_srv - t0)
    t_rep = time.monotonic()
    _pump_replies(plan, deadline)
    t_srv = time.monotonic()
    t_rep = t_srv - t_rep
    while plan.writes and _refused and (deadline - time.monotonic()
                                        >= link_health.MIN_SLICE_S + plan.close_s):
        _close(_refused.pop(), WS_TRY_AGAIN_LATER)
        stats["refused"] += 1
        _note("client_refused", reason="max %d clients" % MAX_WS_CLIENTS)
        print("%.3f WS client refused: max %d %s"
              % (time.monotonic(), MAX_WS_CLIENTS, _count()))
    while _new:
        ws = _new.pop(0)
        if ws in _clients:
            _begin_replay(ws, _path.plan(time.monotonic()))
    now = time.monotonic()
    for ws in list(_clients):
        m = _meta.get(id(ws))
        if plan.writes and m is not None and m["todo"] and now >= m["next_at"] \
                and ws in _clients and m["path"].writes():
            _pump(ws, m, plan, now, deadline)
    t_new = time.monotonic()
    now = t_new
    clients = list(_clients)
    _turn[0] += 1
    if clients:                                     # rotate: a spent budget must not starve the last
        r = _turn[0] % len(clients)
        clients = clients[r:] + clients[:r]
    for ws in clients:
        if ws not in _clients:
            continue
        if deadline - time.monotonic() < link_health.FRAME_BOUND_S + plan.close_s:
            break                                   # reads resume next turn (silence limit is 20 s)
        try:
            _receive(ws, now)
        except Exception as e:
            _event(ws, "failed", e)
            continue
        if ws.closed:
            _event(ws, "closed")
            continue
        m = _meta.get(id(ws))
        if m is not None and m["hb"] and now - m["rx"] > CLIENT_SILENT_S:
            _event(ws, "silent", "silent %d s" % CLIENT_SILENT_S)
        elif m is not None and m["hb"] and now - m["rx"] > CLIENT_LAG_S:
            _event(ws, "quiet")
    t_end = time.monotonic()
    took = t_end - t0
    _path.observe(t_end, "poll", took, True)
    _flush_path_events()
    if took > SLOW_POLL_S:
        # the whole Pico waited on the network this long: say so, with the peers
        stats["slow_polls"] = stats.get("slow_polls", 0) + 1
        if took >= STALL_S and _answered[0] > answered0:
            # the client model cut the client(s) behind it (overran / starved):
            # that client's stall, not the radio's, so not one for LinkHealth
            stats["stalls_answered"] += 1
        elif took >= STALL_S:
            _stalls.append(time.monotonic())
            del _stalls[:-8]
        # say which part held the loop: the library's request handling,
        # the replay to a new client, or reading the existing clients
        t_end = time.monotonic()
        print("%.3f ws poll took %.1f s: server %.1f, replay %.1f, read %.1f, of which /status reply %.1f %s"
              % (t_end, took, t_srv - t0, t_new - t_srv, t_end - t_new, t_rep, _count()))
        if _last_route[0]:
            print("  last route %s took %.1f s" % (_last_route[0], _last_route[1]))
        if ws_bounds is not None and t_srv - t0 > SLOW_POLL_S:
            b = ws_bounds.last
            print("  server: request read %.1f s, reply send %.1f s (%d B)"
                  % (b["read_s"], b["reply_s"], b["reply_b"]))


def send(obj, keep=True):
    """Send one JSON message to every client; True if at least one got it.
    A failing client is dropped; the others still get the message.
    keep: remember it for replay to the next new client (events yes,
    heartbeats no)."""
    msg = json.dumps(obj)
    if keep:
        _recent.append(msg)
        del _recent[:-REPLAY_N]
    if not _clients:
        return False
    sent = False
    # the whole broadcast is held to the poll budget: a client that cannot
    # take its frame costs at most its own deadline, and whoever is left
    # when the budget is spent gets the frame from the next poll turn
    deadline = time.monotonic() + link_health.POLL_BUDGET_S
    writes_now = _path.plan(time.monotonic()).writes      # SEND_DRAIN: everyone queues
    for ws in list(_clients):
        if ws not in _clients:
            continue
        if ws.closed:
            _event(ws, "closed")
            continue
        m = _meta.get(id(ws))
        left = deadline - time.monotonic()
        writes = m is None or m["path"].writes()
        if m is not None and (m["todo"] or not writes or not writes_now
                              or left < link_health.MIN_SLICE_S):
            # a replay is still going out, the client is LAGGING (hold), or
            # the budget is spent: keep the order, queue behind.  Heartbeats
            # (keep=False) are state, not events: skip them.
            if keep:
                if len(m["todo"]) >= TODO_MAX:
                    _event(ws, "backlog", "replay backlog")
                    continue
                m["todo"].append(msg)
            sent = sent or writes
            continue
        if _deliver(ws, msg, min(SEND_TIMEOUT_S, left), "live"):
            sent = True
    return sent


def ws_connected():
    return bool(_clients)


def ws_clients():
    return len(_clients)


def live_clients(now=None):
    """Heartbeat clients heard within CLIENT_SILENT_S (Sandbells ingest,
    page 3)."""
    now = time.monotonic() if now is None else now
    return sum(1 for ws in _clients
               if _meta.get(id(ws), {}).get("hb")
               and now - _meta[id(ws)]["rx"] <= CLIENT_SILENT_S)


def sandbells_connected():
    """LED1 green.  Once any client has sent a heartbeat this boot, only a
    live heartbeat client counts (a half-open socket no longer shows green);
    before that (old Sandbells), any client counts, as before."""
    if stats["hb_seen"]:
        return live_clients() > 0
    return bool(_clients)


def stalls_since(now, window_s):
    """How many polls took STALL_S or more within the last window_s seconds."""
    return len([t for t in _stalls if now - t <= window_s])


def last_rx_age_s(now=None):
    """Seconds since any client last sent us a frame (heartbeat); None if
    nothing has been heard this boot."""
    now = time.monotonic() if now is None else now
    at = stats["last_rx_at"]
    return None if at is None else round(now - at, 1)


def reset_radio():
    """Wi-Fi looks up but nothing gets through: radio off/on so the next
    join starts clean.  Clients and the server go first (their sockets are
    on the old link)."""
    del _stalls[:]
    stop_server()
    try:
        wifi.radio.enabled = False
        time.sleep(1)
        wifi.radio.enabled = True
    except Exception as e:
        print("radio reset:", e)


def _by_state():
    """{"LIVE": 1, "LAGGING": 1} for /status and the heartbeat."""
    out = {}
    for ws in _clients:
        m = _meta.get(id(ws))
        st = m["path"].state if m is not None else "?"
        out[st] = out.get(st, 0) + 1
    return out


def link_status():
    """For /status and the heartbeat: counters plus the last drop."""
    now = time.monotonic()
    d = dict(stats)
    d["clients"] = len(_clients)
    d["live"] = live_clients(now)
    d["rx_age_s"] = last_rx_age_s(now)
    d["send_state"] = _path.state
    d["send_level"] = _path.level
    d["clients_by_state"] = _by_state()
    if d["last_drop_at"] is not None:
        d["last_drop_age_s"] = round(now - d["last_drop_at"], 1)
    del d["last_drop_at"]
    del d["last_rx_at"]
    return d


def reset_state():
    """Tests: forget clients, notes, replay buffer and counters."""
    del _clients[:]
    del _refused[:]
    del _new[:]
    del _recent[:]
    del _stalls[:]
    del _notes[:]
    del _replies[:]
    _gate.reset()
    _answered[0] = 0
    _path.reset()
    _meta.clear()
    for k in stats:
        stats[k] = 0
    stats["last_drop_reason"] = None
    stats["last_drop_at"] = None
    stats["last_rx_at"] = None
    stats["last_reply_s"] = stats["last_reply_b"] = None
    stats["hb_seen"] = False
