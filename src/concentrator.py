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
        pixel = status_pixel(
            getattr(board, "GP%d" % pinmap["neopixel_gp"]), n=1
        )
        self.tower = TowerWatch(self.n, status_pixel=pixel)
        fitted = set(pinmap.get("fitted", []))
        
        for i in range(self.n):
            self.tower.set_fitted(i, i in fitted)
        self.last_false = [None] * self.n
        self.edge_at = [None] * self.n
        self._prev_held = [False] * self.n
        self._ran = False
        print(
            "concentrator sense", self.sense_gps,
            "neo", pinmap["neopixel_gp"],
            "test", self.test_program,
        )

    def gps_day(self):
        """Call when the GPS clock head announces the day boundary."""
        self.tower.gps_tick()

    def report_head(self, index, kind, return_held):
        self.tower.report(index, kind, return_held)

    def poll_sense(self):
        """Active-low sense, pull-up. True return means the reflector is in the beam.

        A falling edge opens a ring window. A second edge inside it is a
        blow report. A return held with no edge is STOOD, not an edge.
        """
        now = time.monotonic()
        for i, pin in enumerate(self.inputs):
            held = not pin.value
            became = held and not self._prev_held[i]
            self._prev_held[i] = held
            if became and self.last_false[i] is not None:
                self.edge_at[i] = now
                print("ch %d EDGE" % (i + 1))
            if not held:
                self.last_false[i] = now
                if self.tower.test_mark[i]:
                    self.tower.clear_test(i)
            kind = IDLE
            if self.edge_at[i] is not None and (now - self.edge_at[i]) < RING_WINDOW_S:
                kind = RINGING
            elif held:
                kind = STOOD
            if became:
                print("ch %d HELD kind=%s" % (i + 1, kind))
            elif not held and self.edge_at[i] is not None:
                print("ch %d CLEAR" % (i + 1))
            self.tower.report(i, kind, held)

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
    cfg = load_settings()
    Concentrator(cfg).loop()


if __name__ == "__main__" or __name__ == "<module>":
    main()
