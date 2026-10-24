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
#   * Every connect / drop / refusal is counted, kept as last_drop_reason,
#     and queued as a link note: concentrator.py turns notes into "link"
#     events (take_link_notes()).
import json
import time
import wifi
import adafruit_connection_manager

try:
    from adafruit_httpserver import Server, Request, Response, GET, Websocket
    HAS_HTTP = True
except ImportError:
    HAS_HTTP = False

MAX_WS_CLIENTS = 4             # RAM: one socket + small buffers each
WS_NORMAL = 1000               # close codes (RFC 6455)
WS_GOING_AWAY = 1001
WS_TRY_AGAIN_LATER = 1013

CLIENT_SILENT_S = 20.0         # a heartbeat client silent this long is dead
SEND_TIMEOUT_S = 1.0           # most one send may block on a full TCP buffer
RX_FRAMES_PER_POLL = 4         # frames read per client per poll()
REPLAY_N = 32                  # recent events replayed to each new client
NOTES_MAX = 16                 # link notes waiting for concentrator.py
EAGAIN = 11                    # errno values (CircuitPython has no errno module
ETIMEDOUT = 110                # on every port, so plain numbers)

_server = None
_host = None                   # IP the server is bound to
_cfg = None
_clients = []                  # live Websocket objects, oldest first
_refused = []                  # accepted over the limit; closed in poll()
_new = []                      # accepted this poll: replay + note in poll()
_meta = {}                     # id(ws) -> {"at", "rx", "hb"}
_recent = []                   # last REPLAY_N event messages (json str)
_notes = []                    # (what, info) for concentrator.py
_payload_fn = lambda: {}
stats = {"connects": 0, "drops": 0, "refused": 0, "send_timeouts": 0,
         "silent_drops": 0, "last_drop_reason": None, "last_drop_at": None,
         "hb_seen": False, "last_rx_at": None}


def wifi_candidates(cfg):
    nets = (cfg.raw.get("wifi") or {}).get("networks") or []
    if nets:
        return sorted(nets, key=lambda n: n.get("priority", 99))
    ssid = cfg.get_str("wifi.ssid", "")
    pw = cfg.get_str("wifi.password", "")
    return [{"ssid": ssid, "password": pw}] if ssid else []


_joined_ssid = None   # remembered because ap_info is not available on every build


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
        pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
        server = Server(pool, debug=False)
        port = cfg.get_int("network.http_port", 80)
        path_status = cfg.get_str("network.status_path", "/status")
        path_ws = cfg.get_str("network.ws_path", "/ws")

        @server.route(path_status, GET)
        def status_route(request: Request):
            try:
                body = json.dumps(_payload_fn())
            except Exception as e:
                print("status payload error", e)
                body = '{"error":"payload"}'
            return Response(
                request,
                body=body,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        @server.route(path_ws, GET)
        def ws_route(request: Request):
            return _accept(Websocket(request))

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


def _accept(ws):
    """Add a client, or queue it for a polite refusal when full.  The
    handshake must go out first, so the refusal close (and the replay of
    recent events to a new client) is sent in poll()."""
    if len(_clients) >= MAX_WS_CLIENTS:
        _refused.append(ws)
    else:
        _clients.append(ws)
        _meta[id(ws)] = {"at": time.monotonic(), "rx": None, "hb": False}
        _new.append(ws)
        stats["connects"] += 1
        print("%.3f WS client connected %s" % (time.monotonic(), _count()))
    return ws


CLOSE_TIMEOUT_S = 0.2          # a close frame to a stuck peer is not worth more


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
        try:
            payload = b"" if code is None else int(code).to_bytes(2, "big")
            _send_frame(ws, 8, payload, CLOSE_TIMEOUT_S)     # 8 = CLOSE
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass
    ws.closed = True


_ERRNO_WORDS = {32: "broken pipe", 104: "connection reset", 107: "not connected",
                110: "timed out", 113: "no route to host", 128: "not connected"}


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
        elif time.monotonic() > deadline:
            raise OSError(ETIMEDOUT, "send timeout %.1f s" % timeout_s)


def _send_text(ws, msg):
    """One text frame with a deadline.  adafruit_httpserver 4.8.2
    send_message() retries EAGAIN forever (Response._send_bytes), so a dead
    peer with a full TCP buffer hangs everything; this gives up after
    SEND_TIMEOUT_S and raises, so the caller drops the client.  Falls back
    to send_message() if the library internals are not there."""
    prep = getattr(ws, "_prepare_frame", None)
    conn = getattr(getattr(ws, "_request", None), "connection", None)
    if prep is None or conn is None:
        ws.send_message(msg, fail_silently=True)
        return
    try:
        _send_frame(ws, 1, msg.encode("utf-8"), SEND_TIMEOUT_S)   # 1 = TEXT
    except OSError as e:
        if e.errno == ETIMEDOUT:
            stats["send_timeouts"] += 1
        raise


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


def poll():
    """Accept HTTP/WS requests, refuse clients over the limit, replay recent
    events to new clients, then read each client so pings get a pong, a
    close (or a dead socket) is noticed, and a silent heartbeat client is
    dropped.  A bad client is dropped on its own."""
    if _server:
        try:
            _server.poll()
        except Exception as e:
            print("ws poll:", e)
    while _refused:
        _close(_refused.pop(), WS_TRY_AGAIN_LATER)
        stats["refused"] += 1
        _note("client_refused", reason="max %d clients" % MAX_WS_CLIENTS)
        print("%.3f WS client refused: max %d %s"
              % (time.monotonic(), MAX_WS_CLIENTS, _count()))
    while _new:
        ws = _new.pop(0)
        if ws not in _clients:
            continue
        _note("client_connected", peer=_peer(ws), replay=len(_recent))
        for msg in list(_recent):
            try:
                _send_text(ws, msg)
            except Exception as e:
                _drop(ws, e)
                break
    now = time.monotonic()
    for ws in list(_clients):
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
    del _notes[:]
    _meta.clear()
    for k in stats:
        stats[k] = 0
    stats["last_drop_reason"] = None
    stats["last_drop_at"] = None
    stats["last_rx_at"] = None
    stats["hb_seen"] = False
