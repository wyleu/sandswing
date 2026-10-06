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

import board
import digitalio
import neopixel

from tower_watch import (
    TowerWatch,
    status_pixel,
    RINGING,
    STOOD,
    IDLE,
)
from pins_from_settings import load_pinmap

CONFIG_FILE = "settings.json"
RING_WINDOW_S = 0.4
SETTLE_S = 0.3
REST_NEW = (4, 0, 0)    # green
REST_OLD = (0, 0, 4)    # blue
RESPONSE = (0, 48, 0)    # red
OFF = (0, 0, 0)


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


class Concentrator:
    def __init__(self, cfg):
        pinmap = load_pinmap(cfg)
        test = cfg.get("test", {})
        enter = test.get("enter", "watchdog")
        if enter != "watchdog":
            raise ValueError("test.enter must be 'watchdog', got %r" % (enter,))
        self.test_program = test.get("program", "sandswing.py")
        self.pinmap = pinmap
        self.sense_gps = pinmap["sense_gp"]
        self.inputs = _claim_sense(self.sense_gps)
        self.n = len(self.sense_gps)
        self.strip = neopixel.NeoPixel(
                getattr(board, "GP%d" % pinmap["neopixel_gp"]),
                self.n + 2,
                brightness=0.3,
                auto_write=False,
            )
        self.tower = TowerWatch(self.n, status_pixel=self.strip)
        self.strip.fill((0, 0, 0))
        self.strip.show()
        self.fitted = set()
        for i, pin in enumerate(self.inputs):
            if not pin.value:
                self.fitted.add(i)
                self.tower.set_fitted(i, True)             
        self.head = [None] * self.n
        self._still_since = [time.monotonic()] * self.n
        self._still_held = [not pin.value for pin in self.inputs]
        for i in self.fitted:
            self.head[i] = "old"   # fitted at boot means it was already low

        print("fitted sockets", [i + 1 for i in sorted(self.fitted)])
        self.last_false = [None] * self.n
        self.edge_at = [None] * self.n
        self._prev_held = [False] * self.n
        self.high_since = [time.monotonic()] * self.n
        self._ran = False
        print(
            "concentrator sense", self.sense_gps,
            "neo", pinmap["neopixel_gp"],
            "test", self.test_program,
        )
        print("heads", self.head)

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
        if kind is None:
            self.strip[i + 1] = OFF
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


def main():
    app = Concentrator(load_settings())
    try:
        app.loop()
    finally:
        app.strip.fill((0, 0, 0))
        app.strip.show()

if __name__ == "__main__" or __name__ == "<module>":
    main()
