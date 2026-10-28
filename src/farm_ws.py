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

CLIENT_SILENT_S = 20.0         # a heartbeat client silent this long is dead
STALL_S = 5.0                  # a poll pass this slow counts as a stall (link health)
_stalls = []                    # monotonic times of recent stalls, newest last
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
_path = link_health.SendPath() # send-path state model: plan(now) says what is allowed
_payload_fn = lambda: {}
stats = {"connects": 0, "drops": 0, "refused": 0, "send_timeouts": 0,
         "silent_drops": 0, "refused_backoff": 0, "replays_done": 0,
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
        port = cfg.get_int("network.http_port", 80)
        path_status = cfg.get_str("network.status_path", "/status")
        path_ws = cfg.get_str("network.ws_path", "/ws")

        @server.route(path_status, GET)
        def status_route(request: Request):
            t_route = time.monotonic()
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
            return Response(
                request,
                body=body,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

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
        _clients.append(ws)
        _meta[id(ws)] = {"at": time.monotonic(), "rx": None, "hb": False,
                         "todo": [], "left": 0, "next_at": 0.0}
        _new.append(ws)
        stats["connects"] += 1
        print("%.3f WS client connected %s" % (time.monotonic(), _count()))
    return ws


CLOSE_TIMEOUT_S = link_health.CLOSE_TIMEOUT_S   # a close frame to a stuck peer is not worth more


def _close(ws, code):
    """Close politely if the socket takes the close frame within
    CLOSE_TIMEOUT_S, then shut it.  The library's close() sends through the
    same EAGAIN-forever loop as send_message(), so it is only used when the
    internals are missing (tests' fakes)."""
    conn = getattr(getattr(ws, "_request", None), "connection", None)
    if conn is None or getattr(ws, "_prepare_frame", None) is None:
        try:
            ws.close(code)
        except Exception:
            pass
        return
    if not getattr(ws, "closed", False):
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


def _drop(ws, why, code=WS_GOING_AWAY):
    """Forget one client; close its socket quietly; count and note it."""
    known = ws in _clients
    if known:
        _clients.remove(ws)
    m = _meta.pop(id(ws), None)
    if ws in _new:
        _new.remove(ws)
    _close(ws, code)
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
        _note("client_dropped", reason=why,
              hb=bool(m and m["hb"]),
              up_s=None if m is None else round(time.monotonic() - m["at"], 1))
    print("%.3f WS client dropped: %s %s" % (time.monotonic(), why, _count()))


def drop_all(why):
    for ws in list(_clients):
        _drop(ws, why)


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
            if not m["hb"]:
                m["hb"] = True
                stats["hb_seen"] = True


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
    inside the poll budget.  A bad (slow) send drops the client; the model
    has already been told and decides the backoff."""
    sent = 0
    while m["todo"]:
        left = deadline - time.monotonic() - plan.close_s    # keep room to drop it
        if left < link_health.MIN_SLICE_S:
            return                                  # resume next turn
        replaying = m["left"] > 0
        if replaying and sent >= plan.chunk_frames:
            break
        try:
            bad = _send_text(ws, m["todo"][0], min(plan.send_s, left),
                             "replay" if replaying else "live")
        except Exception as e:
            _drop(ws, e)
            return
        if bad and replaying:
            _drop(ws, OSError(ETIMEDOUT, "replay slow"))
            return
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
    plan = _path.plan(t0)
    deadline = t0 + plan.poll_s
    if _server:
        try:
            _server.poll()
        except Exception as e:
            print("ws poll:", e)
    t_srv = time.monotonic()
    _path.observe(t_srv, "server", t_srv - t0, True)
    while _refused and (deadline - time.monotonic()
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
        if m is not None and m["todo"] and now >= m["next_at"] and ws in _clients:
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
            _drop(ws, e)
            continue
        if ws.closed:
            _drop(ws, "closed", WS_NORMAL)
            continue
        m = _meta.get(id(ws))
        if m is not None and m["hb"] and now - m["rx"] > CLIENT_SILENT_S:
            stats["silent_drops"] += 1
            _drop(ws, "silent %d s" % CLIENT_SILENT_S)
    t_end = time.monotonic()
    took = t_end - t0
    _path.observe(t_end, "poll", took, True)
    _flush_path_events()
    if took > SLOW_POLL_S:
        # the whole Pico waited on the network this long: say so, with the peers
        stats["slow_polls"] = stats.get("slow_polls", 0) + 1
        if took >= STALL_S:
            _stalls.append(time.monotonic())
            del _stalls[:-8]
        # say which part held the loop: the library's request handling,
        # the replay to a new client, or reading the existing clients
        t_end = time.monotonic()
        print("%.3f ws poll took %.1f s: server %.1f, replay %.1f, read %.1f %s"
              % (t_end, took, t_srv - t0, t_new - t_srv, t_end - t_new, _count()))
        if _last_route[0]:
            print("  last route %s took %.1f s" % (_last_route[0], _last_route[1]))


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
    for ws in list(_clients):
        if ws.closed:
            _drop(ws, "closed", WS_NORMAL)
            continue
        m = _meta.get(id(ws))
        if m is not None and m["todo"]:
            # a replay is still going out: keep the order, queue behind it.
            # Heartbeats (keep=False) are state, not events: skip them.
            if keep:
                if len(m["todo"]) >= TODO_MAX:
                    _drop(ws, "replay backlog")
                else:
                    m["todo"].append(msg)
            sent = True
            continue
        try:
            _send_text(ws, msg)
            sent = True
        except Exception as e:
            _drop(ws, e)
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


def link_status():
    """For /status and the heartbeat: counters plus the last drop."""
    now = time.monotonic()
    d = dict(stats)
    d["clients"] = len(_clients)
    d["live"] = live_clients(now)
    d["rx_age_s"] = last_rx_age_s(now)
    d["send_state"] = _path.state
    d["send_level"] = _path.level
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
    _path.reset()
    _meta.clear()
    for k in stats:
        stats[k] = 0
    stats["last_drop_reason"] = None
    stats["last_drop_at"] = None
    stats["last_rx_at"] = None
    stats["hb_seen"] = False
