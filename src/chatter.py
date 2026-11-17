"""
chatter.py -- does a head give ONE clean edge per transition?

The chatter test (sandheads page, "Chatter test"; docs/align-scope.md):
sandtest turns a vane through a head's beam at a steady rate, and this
counts what the stroke path really sees -- the same keypad edges, with
their own stamps -- so the laser drive and PWM speed can be judged on
numbers, not on a theory.

Edges on a channel closer together than gap_ms are one BURST.  A burst with
an odd number of edges changed the level: a TRANSITION (1 edge: clean; more:
chatter at the edge).  A burst with an even number left the level as it was:
SPURIOUS edges in a steady state (a glitch or flicker with the beam clear or
blocked).  Per channel: transitions, clean ones, most edges in one
transition, the edge total (for the mean), longest transition burst (ms),
spurious edges.

Cheap in the stroke path: edge() is a few integer compares and stores into
preallocated lists (small ints: no allocation), and only while counting.
The module is imported only when a test starts (tune.Tuner.chatter_cmd).
Everything is RAM only.

Times are the concentrator's extended tick ms (the keypad edge stamps), so
the burst length is to 1 ms and an edge pair shorter than the keypad scan
(inputs.scan_interval_ms) is not seen at all: the counts are what the
stroke path sees, not what a scope on the comparator would see.

Settings ("tune"): chatter_gap_ms.
"""
from ring import MIN_STROKE_GAP_MS

# Edges closer than this are one transition.  A real strip passes in tens of
# ms (two transitions about 60 ms apart), so 20 ms never merges the two
# edges of a real pass, and comparator chatter (ms or less) is well inside.
CHATTER_GAP_MS = 20
CHATTER_GAP_MIN_MS = 1
CHATTER_GAP_MAX_MS = MIN_STROKE_GAP_MS     # longer: two strokes would be one burst
MEAN_SCALE = 10                            # the mean is reported x10 (one decimal)


def gap_setting(cfg, given=None):
    """given (a command's gap_ms), else settings "tune".chatter_gap_ms, else
    CHATTER_GAP_MS; outside the limits: ValueError for a given value, the
    default for a bad setting."""
    if given is not None:
        if isinstance(given, bool) or not isinstance(given, int) or not (
                CHATTER_GAP_MIN_MS <= given <= CHATTER_GAP_MAX_MS):
            raise ValueError("bad gap")
        return given
    sec = cfg.get("tune") if cfg is not None and hasattr(cfg, "get") else None
    v = sec.get("chatter_gap_ms") if isinstance(sec, dict) else None
    if isinstance(v, bool) or not isinstance(v, int) or not (
            CHATTER_GAP_MIN_MS <= v <= CHATTER_GAP_MAX_MS):
        return CHATTER_GAP_MS
    return v


class Chatter:
    """Per-channel edge bursts.  edge(i, t) from the sense path."""

    def __init__(self, n, gap_ms=CHATTER_GAP_MS):
        self.n = n
        self.gap_ms = gap_ms
        self.on = False
        self.started = None              # tick ms of the last start
        z = [0] * n
        self.b_first = list(z)           # the open burst: first edge ms
        self.b_last = list(z)            # ... last edge ms
        self.b_n = list(z)               # ... edges (0: no open burst)
        self.trans = list(z)
        self.clean = list(z)
        self.max_edges = list(z)
        self.sum_edges = list(z)         # edges in transitions (the mean)
        self.max_ms = list(z)            # longest transition burst
        self.spurious = list(z)          # edges in bursts that left the level as it was

    def start(self, now, gap_ms=None):
        for i in range(self.n):
            self.b_n[i] = self.trans[i] = self.clean[i] = 0
            self.max_edges[i] = self.sum_edges[i] = self.max_ms[i] = self.spurious[i] = 0
        if gap_ms is not None:
            self.gap_ms = gap_ms
        self.started = now
        self.on = True

    def edge(self, i, t):
        """One comparator edge (either way) on channel i (0-based) at tick ms t."""
        if not self.on or not 0 <= i < self.n:
            return
        k = self.b_n[i]
        if k and t - self.b_last[i] >= self.gap_ms:
            self._close(i)
            k = 0
        if not k:
            self.b_first[i] = t
        self.b_n[i] = k + 1
        self.b_last[i] = t

    def _close(self, i):
        k = self.b_n[i]
        self.b_n[i] = 0
        if not k:
            return
        if k & 1:
            self.trans[i] += 1
            if k == 1:
                self.clean[i] += 1
            if k > self.max_edges[i]:
                self.max_edges[i] = k
            self.sum_edges[i] += k
            d = self.b_last[i] - self.b_first[i]
            if d > self.max_ms[i]:
                self.max_ms[i] = d
        else:
            self.spurious[i] += k

    def settle(self, now):
        """Close every burst with no edge for gap_ms (a report counts only
        finished bursts)."""
        for i in range(self.n):
            if self.b_n[i] and now - self.b_last[i] >= self.gap_ms:
                self._close(i)

    def stop(self, now):
        """Stop counting; an open burst is closed as it stands."""
        for i in range(self.n):
            self._close(i)
        self.on = False

    def summary(self, i, now):
        """Channel i -> (transitions, clean, max edges, mean edges x10 or
        None, longest transition ms, spurious edges)."""
        if self.on:
            self.settle(now)
        t = self.trans[i]
        mean = (self.sum_edges[i] * MEAN_SCALE + t // 2) // t if t else None
        return (t, self.clean[i], self.max_edges[i], mean, self.max_ms[i], self.spurious[i])
