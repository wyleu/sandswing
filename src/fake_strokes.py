# fake_strokes.py -- BENCH ONLY: regular fake "stroke" events on /ws.
# Stop code.py with Ctrl-C, Enter for the REPL, then:
#   import fake_strokes
#   fake_strokes.run()                 # ch 1 every 2 s until Ctrl-C
#   fake_strokes.run(ch=2, every_s=1.5, count=20)
# Ctrl-D afterwards restarts the real code.py.
import os
import time
import wifi
import farm_ws
from config_loader import load_config
from sand_event import Emitter
from sntp_pico import TickClock


def _clock(cfg, ip):
    try:
        import socketpool
        from sntp_pico import SntpClock
        from time_servers import candidates
        t = cfg.get("time", {}) or {}
        ips = candidates(t.get("ntp_ips"), ip, wifi.radio.ipv4_subnet, log=print)
        if not ips:
            print("no time servers - strokes go out unsynced")
            return None
        c = SntpClock(socketpool.SocketPool(wifi.radio), ips)
        c.burst_once()
        return c
    except Exception as e:
        print("no time sync:", e)
        return None


def run(ch=1, every_s=2.0, count=0):
    cfg = load_config()
    if wifi.radio.ipv4_address is None:
        farm_ws.connect_wifi(cfg)
    ip = wifi.radio.ipv4_address
    print("IP", ip)
    if ip is None:
        print("no WiFi - stopping")
        return
    clock = _clock(cfg, ip)
    farm_ws.start_server(cfg, lambda: {"fake_strokes": True, "ch": ch})
    ticks = TickClock()
    ev = Emitter(src="sandswing",
                 device_id=cfg.get("device", {}).get("name", "concentrator"),
                 boot_id=os.urandom(4).hex(), ticks=ticks)
    n = 0
    due = time.monotonic() + every_s
    try:
        while count == 0 or n < count:
            farm_ws.poll()
            if time.monotonic() >= due:
                n += 1
                ok = farm_ws.send(ev.make(clock, "stroke", {"ch": ch}))
                print("stroke %d ch %d %s" % (n, ch, "sent" if ok else "no client"))
                due += every_s
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    print("stopped after %d strokes" % n)
