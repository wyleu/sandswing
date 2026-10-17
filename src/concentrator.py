"""
concentrator.py
===============
Boot program for the concentrator Pico.

code.py stays the launcher and should name this file in
settings.json -> startup.program. This file does not replace the launcher.

It owns the tower. tower_watch.py decides active, ringing-down, inactive,
the day-tick, the test marks, and the status colour. This file feeds it
head reports and, only after a mark, runs the test program named in
settings.json -> test.program.

settings.json
-------------
  "startup": { "program": "concentrator.py" },
  "test":    { "program": "sandswing.py", "enter": "watchdog" }

enter must be "watchdog". Any other value is refused. A boot does not
drop into the threshold step.

Test program contract
---------------------
sandswing.py remains the threshold step. Preferred: it exposes

    def run(marked):
        ...

and does not call asyncio.run at import. marked is a list of head
indexes. Until that exists, this file execs the named file once, on the
first mark, and will not exec it again after a clean return.

Status
------
Pixel 0 of the NeoPixel is the tower. Colours are set only by
tower_watch.py. This file does not paint the status pixel.
Channel pixels belong to the test program.

Pins
----
The pin list comes from pins_from_settings.load_pinmap. This file does
not read "pins" itself, and it does not call claim_lineup_hardware.
That claim takes the laser PWMs as well as the sense inputs. The test
program needs the lasers, so this file claims sense only, and releases
those inputs before the test program runs.
"""

import time

import asyncio
import board
import digitalio
import neopixel
import wifi

try:
    import farm_log
except ImportError:
    farm_log = None


from config_loader import load_config
from tower_watch import (
    TowerWatch,
    status_pixel,
    RINGING,
    STOOD,
    IDLE,
)
from pins_from_settings import load_pinmap
from farm_ws import connect_wifi, start_server, poll, send
import farm_ws
from head_probe import scan_heads
from sand_status import from_concentrator

CONFIG_FILE = "settings.json"
RING_WINDOW_S = 0.4
SETTLE_S = 0.3
REST_NEW = (4, 0, 0)     # green
REST_OLD = (0, 0, 4)     # blue
RESPONSE = (0, 48, 0)    # red
LOOP_BACK = (4,4, 0)     #cyan
OFF = (0, 0, 0)

server = None

cfg = load_config()
cfg.banner()

def load_settings():
    import json
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def _claim_sense(sense_gps):
    pins = []
    for gp in sense_gps:
        pin = digitalio.DigitalInOut(getattr(board, "GP%d" % gp))
        pin.direction = digitalio.Direction.INPUT
        pin.pull = digitalio.Pull.UP
        pins.append(pin)
    return pins


def _release(pins):
    for pin in pins:
        try:
            pin.deinit()
        except Exception:
            pass

def heartbeat(strip, on):
    strip[-1] = (8, 8, 8) if on else (0, 0, 0)
    
def setup_net():
    if not connect_wifi(cfg):
        emit("No WiFi – lineup still runs")
        return
    import wifi
    for _ in range(20):
        ip = wifi.radio.ipv4_address
        if ip is not None:
            break
        time.sleep(0.25)
    print("IP", ip)
    if ip is None:
        emit("No IP – server not started")
        return
    


if farm_log:
    farm_log.start(cfg)

def emit(msg):
    global _last_line
    _last_line = str(msg)
    print(msg)
    if farm_log:   
        farm_log.write(msg)
        
async def ws_poll_task():
    n = 0
    while True:
        farm_ws.poll()
        n += 1
        if n % 50 == 0:
            print("poll", n, "server", farm_ws._server is not None)
        await asyncio.sleep(0.05)

class Concentrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pinmap = load_pinmap(cfg)
        self._check_test_contract()
        self.sense_gps = self.pinmap["sense_gp"]
        self.n = len(self.sense_gps)          # set n first
        self._setup_sense()          # now claim the sense pins for the rest of the run
        self._setup_strip()
        self._setup_tower()
        self._probe_heads()          # probe claims and releases its own pins
        self._init_edge_state()
        print("heads", self.head)

    def _check_test_contract(self):
        test = self.cfg.get("test", {})
        enter = test.get("enter", "watchdog")
        if enter != "watchdog":
            raise ValueError("test.enter must be 'watchdog', got %r" % (enter,))
        self.test_program = test.get("program", "sandswing.py")

    def _setup_sense(self):
        self.sense_gps = self.pinmap["sense_gp"]
        self.inputs = _claim_sense(self.sense_gps)
        self.n = len(self.sense_gps)

    def _setup_strip(self):
        self.strip = neopixel.NeoPixel(
            getattr(board, "GP%d" % self.pinmap["neopixel_gp"]),
            self.n + 2,
            brightness=0.3,
            auto_write=False,
        )
        self.strip.fill((0, 0, 0))
        self.strip.show()

    def _setup_tower(self):
        self.tower = TowerWatch(self.n, status_pixel=self.strip)

    def _probe_heads(self):
        from head_probe import scan_heads
        self.head = scan_heads(self.pinmap, self.cfg, self.inputs)
        self.fitted = set()
        for i, kind in enumerate(self.head):
            if kind in ("old", "new", "lb"):
                self.fitted.add(i)
                self.tower.set_fitted(i, True)
            print("ch %d %s" % (i + 1, kind))

    def _init_edge_state(self):
        self._still_since = [time.monotonic()] * self.n
        self._still_held = [not pin.value for pin in self.inputs]
        self.last_false = [None] * self.n
        self.edge_at = [None] * self.n
        self._prev_held = [False] * self.n
        self.high_since = [time.monotonic()] * self.n
        self._ran = False
        
    def status_payload(self):
        print("status hit")
        return from_concentrator({
            "id": self.cfg.get("device", {}).get("name"),
            "name": self.cfg.get("device", {}).get("name"),
            "family": self.cfg.get("device", {}).get("family"),
            "role": self.cfg.get("device", {}).get("role"),
            "location": self.cfg.get("device", {}).get("location"),
            "ip": str(wifi.radio.ipv4_address),
            "ssid": self.cfg.get("wifi", {}).get("ssid"),
            "heads": list(self.head),
            "ts": time.time(),
        })


    def gps_day(self):
        """Call when the GPS clock head announces the day boundary."""
        self.tower.gps_tick()

    def report_head(self, index, kind, return_held):
        self.tower.report(index, kind, return_held)

    def poll_sense(self):
        """Active-low sense, pull-up. A falling edge opens a ring window.
        A return held with no edge is STOOD, not an edge.
        """
        now = time.monotonic()
        for i, pin in enumerate(self.inputs):
            kind, held = self._channel(i, pin, now)
            self._pixel(i, held)
        heartbeat(self.strip, int(now * 2) % 2)
        self.strip.show()
        
    def _settle(self, i, held, now):
        if held != self._still_held[i]:
            self._still_held[i] = held
            self._still_since[i] = now
            return
        if i not in self.fitted:
            return
        if (now - self._still_since[i]) < SETTLE_S:
            return
        kind = "old" if held else "new"
        if self.head[i] is None:
            self.head[i] = kind
            print("ch %d head=%s" % (i + 1, kind))
            
    def _channel(self, i, pin, now):
        prev = self._prev_held[i]
        held = not pin.value
        became = held and not prev
        released = (not held) and prev
        self._prev_held[i] = held
        if became and self.last_false[i] is not None:
            self.edge_at[i] = now
            print("ch %d EDGE" % (i + 1))
        if not held:
            self.last_false[i] = now
            if self.tower.test_mark[i]:
                self.tower.clear_test(i)
        if self.edge_at[i] is not None and (now - self.edge_at[i]) < RING_WINDOW_S:
            kind = RINGING
        elif held:
            kind = STOOD
        else:
            kind = IDLE
        if became:
            print("ch %d HELD kind=%s" % (i + 1, kind))
        elif released:
            print("ch %d CLEAR" % (i + 1))
        if held and i not in self.fitted:
            self.fitted.add(i)
            self.tower.set_fitted(i, True)
            print("ch %d fitted" % (i + 1))
        self.tower.report(i, kind, held)
        self._settle(i, held, now)
        return kind, held

    def _pixel(self, i, held):
        kind = self.head[i]
        if kind is None or kind == "empty":
            self.strip[i + 1] = OFF
            return
        if kind == "lb":
            self.strip[i + 1] = LOOP_BACK
            return
        resting_held = kind == "old"
        if held != resting_held:
            self.strip[i + 1] = RESPONSE
        elif kind == "new":
            self.strip[i + 1] = REST_NEW
        else:
            self.strip[i + 1] = REST_OLD
            
    def maybe_test(self):
        marked = self.tower.marked()
        if not marked or self._ran:
            return
        self.tower.mark_test_running(True)
        _release(self.inputs)
        self.inputs = []
        try: 
            self._run_test(marked)
        finally:
            self.tower.mark_test_running(False)
            self._ran = True

    def _run_test(self, marked):
        name = self.test_program
        try:
            mod_name = name[:-3] if name.endswith(".py") else name
            mod = __import__(mod_name)
            run = getattr(mod, "run", None)
            if run is not None:
                run(marked)
                return
        except Exception as e:
            print("test import:", e)
        print("exec", name, "marked", marked)
        with open(name, "r") as f:
            exec(f.read(), {"__name__": "__test__"})

    def loop(self):
        while True:
            self.poll_sense()
            self.tower.poll()
            self.maybe_test()
            time.sleep(0.02)
            
    async def sense_task(self):
        while True:
            self.poll_sense()
            self.tower.poll()
            self.maybe_test()
            await asyncio.sleep(0.02)

    async def status_task(self):
        while True:
            # existing status / WS send
            await asyncio.sleep(0.1)


async def main():
    setup_net()
    app = Concentrator(cfg)
    start_server(cfg, app.status_payload)
    try:
        await asyncio.gather(
            app.sense_task(),
            app.status_task(),
            ws_poll_task(),
        )
    finally:
        app.strip.fill((0, 0, 0))
        app.strip.show()
        
        if farm_log:
            try:
                farm_log.close()
            except Exception:
                pass
        print("Cleanup complete")

if __name__ == "__main__" or __name__ == "<module>":
    asyncio.run(main())
