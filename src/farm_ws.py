# farm_ws.py — HTTP /status + up to MAX_WS_CLIENTS /ws clients (Sand* farm)
#
# Shared by sandswing, sandsense, and other timed Sand* devices.
# Pattern for /ws accept taken from sandsense (Websocket(request)).
# Every client gets every send (sandstore, sandbells, debug listeners).
# Over the limit the NEW client is refused (accepted, then closed with
# 1013 "try again later"): long-lived clients such as sandstore are never
# kicked off by an ad-hoc listener.
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

_server = None
_clients = []                  # live Websocket objects, oldest first
_refused = []                  # accepted over the limit; closed in poll()
_payload_fn = lambda: {}


def wifi_candidates(cfg):
    nets = (cfg.raw.get("wifi") or {}).get("networks") or []
    if nets:
        return sorted(nets, key=lambda n: n.get("priority", 99))
    ssid = cfg.get_str("wifi.ssid", "")
    pw = cfg.get_str("wifi.password", "")
    return [{"ssid": ssid, "password": pw}] if ssid else []


def connect_wifi(cfg):
    last = None
    for n in wifi_candidates(cfg):
        ssid, password = n.get("ssid", ""), n.get("password", "")
        if not ssid:
            continue
        try:
            print("Trying WiFi:", ssid)
            wifi.radio.connect(ssid, password)
            print("WiFi OK", ssid, wifi.radio.ipv4_address)
            return True
        except Exception as e:
            last = e
            print("Fail:", ssid, e)
    print("WiFi failed:", last)
    return False


def start_server(cfg, payload_fn):
    """payload_fn() -> dict for GET /status. Also accepts /ws clients
    (up to MAX_WS_CLIENTS)."""
    global _server, _payload_fn
    _payload_fn = payload_fn
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
        return server
    except Exception as e:
        print("WS server failed:", e)
        _server = None
        return None


def _count():
    return "(%d client%s)" % (len(_clients), "" if len(_clients) == 1 else "s")


def _accept(ws):
    """Add a client, or queue it for a polite refusal when full.  The
    handshake must go out first, so the refusal close is sent in poll()."""
    if len(_clients) >= MAX_WS_CLIENTS:
        _refused.append(ws)
    else:
        _clients.append(ws)
        print("%.3f WS client connected %s" % (time.monotonic(), _count()))
    return ws


def _close(ws, code):
    try:
        ws.close(code)
    except Exception:
        pass


def _drop(ws, why, code=WS_GOING_AWAY):
    """Forget one client; close its socket quietly."""
    if ws in _clients:
        _clients.remove(ws)
    _close(ws, code)
    print("%.3f WS client dropped: %s %s" % (time.monotonic(), why, _count()))


def poll():
    """Accept HTTP/WS requests, refuse clients over the limit, then read one
    frame from each client so pings get a pong and a close (or a dead
    socket) is noticed.  A bad client is dropped on its own."""
    if _server:
        try:
            _server.poll()
        except Exception as e:
            print("ws poll:", e)
    while _refused:
        _close(_refused.pop(), WS_TRY_AGAIN_LATER)
        print("%.3f WS client refused: max %d %s"
              % (time.monotonic(), MAX_WS_CLIENTS, _count()))
    for ws in list(_clients):
        try:
            ws.receive(fail_silently=True)    # handles ping/pong/close; data ignored
        except Exception as e:
            _drop(ws, e)
            continue
        if ws.closed:
            _drop(ws, "closed", WS_NORMAL)


def send(obj):
    """Send one JSON message to every client; True if at least one got it.
    A failing client is dropped; the others still get the message."""
    if not _clients:
        return False
    msg = json.dumps(obj)
    sent = False
    for ws in list(_clients):
        if ws.closed:
            _drop(ws, "closed", WS_NORMAL)
            continue
        try:
            ws.send_message(msg, fail_silently=True)
            sent = True
        except Exception as e:
            _drop(ws, e)
    return sent


def ws_connected():
    return bool(_clients)


def ws_clients():
    return len(_clients)
