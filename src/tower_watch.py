"""
tower_watch.py
==============
Tower watchdog for the concentrator Pico.

This module owns the tower. It does not sweep a threshold, emit a blow,
speak MIDI, or launch a program. code.py stays the launcher. sandswing.py
stays the optical step. The concentrator program calls this module, and
only runs the lineup after a head has been marked.

What it decides
---------------
Ringing is an edge inside the ringing window. The tower is active while
any fitted bell is ringing.

Inactive is not a long stand on one head. It is the whole tower:
  * no edge from any bell for RINGDOWN_S (10 s) after ringing down, or
  * sooner, when every fitted bell is stood.

A return held true is not an edge. A strip left in the beam does not
keep the tower active.

The day belongs to the GPS clock head. This module takes that tick.
Until the clock head is connected it counts DAY_S (86400) from boot and
flags the tick as local, not clock-aligned.

On a tick:
  * tower active   -> tick dropped, one short white flash, stay green
  * tower inactive -> every fitted head whose return is still true is
                      marked for test mode

Test mode ends outside this module, when that head's return goes false
and stays false (the non-reflective patch fitted on the rested strip).
The concentrator clears the mark by calling clear_test().

Status pixel
------------
The status WS2812 is the tower, not a channel. One colour at a time.
Channel pixels are not driven here.

  boot, no state yet                              red
  active, bells ringing                           green
  ringing down, inside the 10 s                   amber
  inactive, day not yet ticked                    blue
  inactive, local day-tick only                   white
  inactive, GPS day-tick, a head marked for test  magenta
  test mode (marked head actually running)        cyan

Red is boot only. It leaves as soon as the module has a state.
A dropped day-tick is one short white flash, then back to green.
"""

import time

try:
    import neopixel
except ImportError:
    neopixel = None

RINGDOWN_S = 10
DAY_S = 86400
FLASH_S = 0.15

RED = (48, 0, 0)
GREEN = (0, 48, 0)
AMBER = (24, 48, 0)
BLUE = (0, 0, 48)
WHITE = (32, 32, 32)
MAGENTA = (48, 0, 48)
CYAN = (0, 48, 48)
OFF = (0, 0, 0)

# Per-bell reports from a head. A held return is not an edge.
RINGING = "ringing"
STOOD = "stood"
IDLE = "idle"

# Tower
ACTIVE = "active"
RINGING_DOWN = "ringing_down"
INACTIVE = "inactive"
BOOT = "boot"


class TowerWatch:
    """Top-level tower model. Feed it head reports. Read state and marks."""

    def __init__(self, n_bells, status_pixel=None, monotonic=None):
        self.n = n_bells
        self.now = monotonic or time.monotonic
        self.status = status_pixel
        self.bell = [IDLE] * n_bells
        self.ret_held = [False] * n_bells
        self.fitted = [True] * n_bells
        self.tower = BOOT
        self.last_edge = None
        self.boot_at = self.now()
        self.day_origin = self.boot_at
        self.gps_day = False
        self.ticked = False
        self.test_mark = [False] * n_bells
        self.test_running = False
        self._show(RED)

    def set_fitted(self, index, fitted):
        self.fitted[index] = bool(fitted)

    def report(self, index, kind, return_held):
        """One head, one report.

        kind is RINGING, STOOD, or IDLE.
        return_held is true when the reflector is in the beam.
        RINGING is the only report that counts as an edge.
        """
        self.bell[index] = kind
        self.ret_held[index] = bool(return_held)
        if kind == RINGING:
            self.last_edge = self.now()
            self.ticked = False
        self._reduce()

    def gps_tick(self):
        """Day boundary from the GPS clock head. Aligns the local count."""
        self.gps_day = True
        self.day_origin = self.now()
        self._on_tick(local=False)

    def poll(self):
        """Call from the concentrator loop. Handles ring-down and the local day."""
        self._reduce()
        if not self.gps_day and (self.now() - self.day_origin) >= DAY_S:
            self.day_origin = self.now()
            self._on_tick(local=True)
        return self.tower

    def mark_test_running(self, running):
        """Concentrator sets this while a marked head is in the threshold step."""
        self.test_running = bool(running)
        self._paint()

    def clear_test(self, index):
        """Return went false and stayed false. Patch fitted, or beam empty."""
        self.test_mark[index] = False
        if not any(self.test_mark):
            self.test_running = False
        self._paint()

    def marked(self):
        return [i for i, m in enumerate(self.test_mark) if m]

    def _reduce(self):
        if self.last_edge is None:
            self.tower = INACTIVE
            self._paint()
            return
        if any(self.bell[i] == RINGING for i in self._fitted()):
            self.tower = ACTIVE
            self._paint()
            return
        if self._all_stood():
            self.tower = INACTIVE
            self._paint()
            return
        if (self.now() - self.last_edge) >= RINGDOWN_S:
            self.tower = INACTIVE
        else:
            self.tower = RINGING_DOWN
        self._paint()

    def _on_tick(self, local):
        if self.tower == ACTIVE or self.tower == RINGING_DOWN:
            self._flash_dropped()
            return
        self.ticked = True
        for i in self._fitted():
            if self.ret_held[i]:
                self.test_mark[i] = True
        self._paint()

    def _fitted(self):
        return [i for i in range(self.n) if self.fitted[i]]

    def _all_stood(self):
        fitted = self._fitted()
        return bool(fitted) and all(self.bell[i] == STOOD for i in fitted)

    def _paint(self):
        if self.test_running:
            self._show(CYAN)
            return
        if any(self.test_mark) and self.gps_day:
            self._show(MAGENTA)
            return
        if self.tower == ACTIVE:
            self._show(GREEN)
        elif self.tower == RINGING_DOWN:
            self._show(AMBER)
        elif self.tower == INACTIVE and self.ticked and not self.gps_day:
            self._show(WHITE)
        elif self.tower == INACTIVE:
            self._show(BLUE)
        else:
            self._show(RED)

    def _flash_dropped(self):
        self._show(WHITE)
        time.sleep(FLASH_S)
        self._show(GREEN)

    def _show(self, colour):
        if colour == getattr(self, "_colour", None):
            return
        self._colour = colour
        names = {
            RED: "red boot",
            GREEN: "green active",
            AMBER: "amber ringing_down",
            BLUE: "blue inactive",
            WHITE: "white tick",
            MAGENTA: "magenta marked",
            CYAN: "cyan test",
        }
        print("tower", names.get(colour, colour), self.tower)
        if self.status is None:
            return
        self.status[0] = colour
        self.status.show()


def status_pixel(pin, n=1):
    """Optional. Status is pixel 0. Channel pixels are not this strip's job
    unless the concentrator has put them on the same object and only asks
    this module to write pixel 0.
    """
    if neopixel is None:
        return None
    return neopixel.NeoPixel(pin, n, brightness=0.3, auto_write=False)
