# stub: no network in the harness
stats = {"hb_seen": False}


def connect_wifi(cfg, timeout=None):
    return False
def start_server(cfg, payload):
    return None
def poll():
    pass
def send(*a, **k):
    pass
def ws_clients():
    return 0
def sandbells_connected():
    return False
def take_link_notes():
    return []
def link_status():
    return {}
def wifi_ok():
    return False
def wifi_ip():
    return None
def wifi_rssi():
    return None
def wifi_ssid():
    return None
def server_running():
    return False
def server_host():
    return None
def stop_server():
    pass
def drop_all(why):
    pass
def last_rx_age_s(now=None):
    return None
def reset_radio():
    pass
