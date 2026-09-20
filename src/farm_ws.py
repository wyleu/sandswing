# farm_ws.py — HTTP /status + one /ws client (Sand* farm)
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
    """payload_fn() -> dict for GET /status."""
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
            return Response(
                request,
                body=json.dumps(_payload_fn()),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        @server.route(path_ws, GET)
        def ws_route(request: Request):
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

def poll():
    if _server:
        try:
            _server.poll()
        except Exception as e:
            print("ws poll:", e)

def send(obj):
    global _ws
    if _ws is None:
        return False
    try:
        _ws.send_message(json.dumps(obj), fail_silently=True)
        return True
    except Exception:
        _ws = None
        print("WS client dropped")
        return False

def ws_connected():
    return _ws is not None