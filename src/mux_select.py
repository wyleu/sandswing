# mux_select.py -- the ONE owner of the 4051 A/B/C select lines (GP18/19/20).
#
# Both 4051s (inward current-sense, outward threshold) share A/B/C, so they
# always sit on the same channel.  Tower claims the lines once; everything
# else borrows a selection:
#
#     with tower.select(bell): ...          (sync; ChannelBusy if held)
#     async with tower.aselect(bell): ...   (waits; optional timeout)
#
# The same bell may re-enter.  Another bell while held raises ChannelBusy:
# never a silent switch.  Shared AnalogIns (GP26 inward COMMON, GP28
# monitor) come from get_analog() so a late user gets the same object.
# Replaces abc_fix/src/mux_select.py (MuxAddress) and the draft's Tower.
import time

from mux_errors import ChannelBusy, MuxConfigError

try:
    import asyncio
except ImportError:
    asyncio = None


def _pins(pins):
    if pins is None:
        from board_pins import BoardPins
        pins = BoardPins()
    return pins


class Tower:
    def __init__(self, a, b, c, inh=None, settle_s=0.001, pins=None, name="tower"):
        self.name = name
        self.pins = _pins(pins)
        self.gps = (int(a), int(b), int(c), None if inh is None else int(inh))
        self.settle_s = settle_s
        self._a = self.pins.output(a, False)
        self._b = self.pins.output(b, False)
        self._c = self.pins.output(c, False)
        # INH high = all switches open. Start inhibited, release on select.
        self._inh = None if inh is None else self.pins.output(inh, True)
        self.current = None      # channel whose lines are driven now
        self.holder = None       # channel that holds the lock, or None
        self.depth = 0           # re-entry count for the holder
        self.selects = 0         # line changes (stats)
        self.warnings = []
        self.pwm_slices = {}
        self._closed = False

    def _drive(self, y):
        y = int(y)
        if not 0 <= y <= 7:
            raise ValueError("4051 channel must be 0..7, got %r" % (y,))
        if y == self.current:
            return False
        if self._inh is not None:
            self._inh.value = True          # break before make
        self._a.value = bool(y & 1)
        self._b.value = bool(y & 2)
        self._c.value = bool(y & 4)
        if self._inh is not None:
            self._inh.value = False
        self.current = y
        self.selects += 1
        return True

    def _take(self, y):
        """Non-blocking acquire. True if the lines changed."""
        if self._closed:
            raise RuntimeError("Tower %s deinit" % self.name)
        y = int(y)
        if self.holder is not None and self.holder != y:
            raise ChannelBusy("tower %s holds ch %d, asked for ch %d"
                              % (self.name, self.holder, y))
        self.holder = y
        self.depth += 1
        try:
            return self._drive(y)
        except Exception:
            self._give()
            raise

    def _give(self):
        self.depth -= 1
        if self.depth <= 0:
            self.depth = 0
            self.holder = None

    def is_held_by(self, y):
        return self.holder is not None and self.holder == int(y) \
            and self.current == int(y)

    def select(self, bell_or_y):
        return _Selection(self, _channel_of(bell_or_y))

    def aselect(self, bell_or_y, timeout_s=None, poll_s=0.002):
        if asyncio is None:
            raise RuntimeError("asyncio not available")
        return _ASelection(self, _channel_of(bell_or_y), timeout_s, poll_s)

    def status(self):
        return {"current": self.current, "holder": self.holder,
                "selects": self.selects, "warnings": list(self.warnings)}

    def deinit(self):
        if self.holder is not None:
            raise ChannelBusy("deinit while ch %d held" % self.holder)
        for p in (self._a, self._b, self._c, self._inh):
            if p is not None:
                try:
                    p.deinit()
                except Exception:
                    pass
        self._closed = True
        for k in list(_TOWERS):
            if _TOWERS[k] is self:
                del _TOWERS[k]


def _channel_of(x):
    return int(getattr(x, "y", x))


class _Selection:
    def __init__(self, tower, y):
        self.tower, self.y = tower, y

    def __enter__(self):
        if self.tower._take(self.y):
            self.tower.pins.sleep(self.tower.settle_s)
        return self.tower

    def __exit__(self, et, ev, tb):
        self.tower._give()
        return False


class _ASelection:
    def __init__(self, tower, y, timeout_s, poll_s):
        self.tower, self.y = tower, y
        self.timeout_s, self.poll_s = timeout_s, poll_s

    async def __aenter__(self):
        t0 = time.monotonic()
        while True:
            try:
                changed = self.tower._take(self.y)
                break
            except ChannelBusy:
                if self.timeout_s is not None and \
                        time.monotonic() - t0 >= self.timeout_s:
                    raise
                await asyncio.sleep(self.poll_s)
        if changed:
            await asyncio.sleep(self.tower.settle_s)
        return self.tower

    async def __aexit__(self, et, ev, tb):
        self.tower._give()
        return False


# ---- registry: one Tower per select group, one AnalogIn per GP ----------
_TOWERS = {}
_ADC = {}


def get_tower(name, spec, pins=None):
    """One Tower per name.  Same pins again -> same object; a different
    name reusing any select pin -> MuxConfigError (not 'GPxx in use')."""
    gps = (int(spec["a"]), int(spec["b"]), int(spec["c"]),
           None if spec.get("inh") is None else int(spec["inh"]))
    t = _TOWERS.get(name)
    if t is not None:
        if t.gps != gps:
            raise MuxConfigError("select group %r redefined %r != %r"
                                 % (name, gps, t.gps))
        return t
    used = set(g for g in gps if g is not None)
    for other in _TOWERS.values():
        clash = used & set(g for g in other.gps if g is not None)
        if clash:
            raise MuxConfigError("select group %r reuses GP%s of %r"
                                 % (name, sorted(clash), other.name))
    settle = float(spec.get("settle_s", spec.get("settle_us", 1000) / 1e6))
    t = Tower(gps[0], gps[1], gps[2], gps[3], settle_s=settle, pins=pins, name=name)
    _TOWERS[name] = t
    return t


def get_address(a, b, c, inh=None, pins=None):
    """Legacy-style lookup by pins (implicit group name)."""
    return get_tower("abc_%s_%s_%s" % (a, b, c),
                     {"a": a, "b": b, "c": c, "inh": inh}, pins)


def get_analog(gp, pins=None):
    """The one AnalogIn for this GP (GP26 inward COMMON, GP28 monitor)."""
    gp = int(gp)
    adc = _ADC.get(gp)
    if adc is None:
        adc = _pins(pins).analog_in(gp)
        _ADC[gp] = adc
    return adc


def reset_registry():
    """Soft restart / tests: deinit every tower and shared AnalogIn."""
    for t in list(_TOWERS.values()):
        t.holder = None
        t.depth = 0
        t.deinit()
    _TOWERS.clear()
    for adc in _ADC.values():
        try:
            adc.deinit()
        except Exception:
            pass
    _ADC.clear()
