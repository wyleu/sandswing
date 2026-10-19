# farm_ws.py — HTTP /status + one /ws client (Sand* farm)
#
# Shared by sandswing, sandsense, and other timed Sand* devices.
# Pattern for /ws accept taken from sandsense (Websocket(request)).
import json
import wifi
import adafruit_connection_manager

try:
    from adafruit_httpserver import Server, Request, Response, GET, Websocket
    HAS_HTTP = True
except ImportError:
    HAS_HTTP = False

_server = None
_ws = None
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
    """payload_fn() -> dict for GET /status. Also accepts one /ws client."""
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
            # One client at a time (sandstore or sandbells). Same pattern as
            # sandsense: replace any previous socket on reconnect.
            global _ws
            if _ws is not None:
                try:
                    _ws.close()
                except Exception:
                    pass
            _ws = Websocket(request)
            print("WS client connected")
            return _ws

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


def _drop(why):
    """Forget the client; close its socket quietly."""
    global _ws
    if _ws is not None:
        try:
            _ws.close()
        except Exception:
            pass
    _ws = None
    print("WS client dropped:", why)


def poll():
    """Accept HTTP/WS requests, then read one frame from the WS client so
    pings get a pong and a client's close (or a dead socket) is noticed."""
    if _server:
        try:
            _server.poll()
        except Exception as e:
            print("ws poll:", e)
    if _ws is not None:
        try:
            _ws.receive(fail_silently=True)   # handles ping/pong/close; we ignore data
        except Exception as e:
            _drop(e)
            return
        if _ws is not None and _ws.closed:
            _drop("closed")


def send(obj):
    """Send one JSON message; False if no client or it has gone."""
    if _ws is None:
        return False
    if _ws.closed:
        _drop("closed")
        return False
    try:
        _ws.send_message(json.dumps(obj), fail_silently=True)
        return True
    except Exception as e:
        _drop(e)
        return False


def ws_connected():
    return _ws is not None
