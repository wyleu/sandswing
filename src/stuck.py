"""
stuck.py -- a head that is "broken" all the time is not a ringing bell.

The tune commands (tune.py) are refused while the tower is not quiet, so a
laser is never changed in the middle of stroke detection.  But a head whose
comparator sits on the threshold (or follows the chopped laser light) gives
a stroke every ring.MIN_STROKE_GAP_MS for ever: the tower never goes quiet,
and the threshold and drive that would cure it can never be changed.  A
lockout.

A real bell is clear (not held) for a good stretch between its strokes.  So
a channel is STUCK when it has had no clear stretch of at least clear_ms for
after_s: held all the time, or flickering faster than any bell.  When every
channel that is keeping the tower awake is stuck (every other channel has
had no edge for ring_down_ms, TowerWatch's ring-down), threshold and drive
commands are let through again.  The stroke path is not changed: a stuck
channel's strokes still go out (flagged on the heartbeat, "tune"."stuck"),
so a wrong verdict never loses a real stroke.

Times are the concentrator's extended tick ms (the keypad edge stamps).

Settings ("tune"): stuck_after_s, stuck_clear_ms.
"""
from ring import MIN_STROKE_GAP_MS
from tower_watch import RINGDOWN_S

MS_PER_S = 1000
# A clear stretch shorter than the minimum stroke gap cannot separate two
# strokes, so it does not show a bell.
STUCK_CLEAR_MS = MIN_STROKE_GAP_MS
# Held / flickering this long.  The tower's own ring-down: a real bell that
# stopped (or stood on its strip) is quiet by then anyway, so no real bell
# is ever let through sooner than before.
STUCK_AFTER_S = RINGDOWN_S
RING_DOWN_MS = RINGDOWN_S * MS_PER_S


def _num(v, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v <= 0:
        return default
    return v


def stuck_settings(cfg):
    """settings "tune": stuck_after_s, stuck_clear_ms -> (after_ms, clear_ms).
    Missing, bad or <= 0: the defaults."""
    sec = cfg.get("tune") if cfg is not None and hasattr(cfg, "get") else None
    sec = sec if isinstance(sec, dict) else {}
    after = _num(sec.get("stuck_after_s"), STUCK_AFTER_S)
    clear = _num(sec.get("stuck_clear_ms"), STUCK_CLEAR_MS)
    return int(after * MS_PER_S), int(clear)


class StuckWatch:
    """Fed every sense edge (held = comparator low, the scope's purple)."""

    def __init__(self, n, cfg=None, born_ms=0, log=print, ring_down_ms=RING_DOWN_MS):
        self.n = n
        self.after_ms, self.clear_ms = stuck_settings(cfg)
        self.ring_down_ms = ring_down_ms
        self.log = log
        self.born = born_ms
        # ms it went clear; None: held.  Clear from the start: a channel's first
        # edge is a break (held) after a clear stretch, or a release
        self.clear_from = [born_ms] * n
        self.long_clear = [None] * n    # ms a clear stretch >= clear_ms last ended / was seen
        self.last_edge = [None] * n     # ms of the last held edge (a stroke or a double)
        self.flagged = [False] * n      # "stuck" logged once per spell
        self.unlocks = 0                # commands let through by this
        self.hushed = 0                 # DOUBLE console lines not printed (a stuck head)

    def edge(self, i, held, t):
        """One edge on channel i (0-based) at tick ms t."""
        if not 0 <= i < self.n:
            return
        if held:
            cf = self.clear_from[i]
            if cf is not None and t - cf >= self.clear_ms:
                self.long_clear[i] = t
                self.flagged[i] = False
            self.clear_from[i] = None
            self.last_edge[i] = t
            if not self.flagged[i] and self.stuck(i, t):
                self.flagged[i] = True
                try:
                    self.log("ch %d stuck: no clear gap of %d ms for %d s; its strokes are "
                             "not a bell; thr / drive allowed" % (
                                 i + 1, self.clear_ms, self.after_ms // MS_PER_S))
                except Exception:
                    pass
        else:
            self.clear_from[i] = t

    def _last_long_clear(self, i, now):
        cf = self.clear_from[i]
        if cf is not None and now - cf >= self.clear_ms:
            return now                  # clear now, and for long enough
        lc = self.long_clear[i]
        return self.born if lc is None else lc

    def stuck(self, i, now):
        if self.last_edge[i] is None:
            return False                # no edge: it cannot be keeping the tower awake
        return now - self._last_long_clear(i, now) >= self.after_ms

    def stuck_list(self, now):
        """1-based channels stuck now (the heartbeat's "tune"."stuck")."""
        return [i + 1 for i in range(self.n) if self.stuck(i, now)]

    def allows(self, now):
        """True when some channel is stuck and every other channel has had no
        edge for ring_down_ms: the tower is awake only because of stuck heads."""
        any_stuck = False
        for i in range(self.n):
            if self.stuck(i, now):
                any_stuck = True
                continue
            e = self.last_edge[i]
            if e is not None and now - e < self.ring_down_ms:
                return False
        return any_stuck
