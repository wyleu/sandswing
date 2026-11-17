"""
sweep.py -- the laser-drive sweep for the sandheads page ("Find best
brightness"): step the drive of the head with the PT wire from max_duty
down to the laser floor, and at each step read the PT level (GP27) and the
average laser current, so the page can draw the drive / brightness curve
and suggest the lowest drive that still gives a good beam.

Cooperative: Aligner.tick calls step() every SWEEP_TICK_S; each call does
at most one ~4 ms GP26 burst (tune.Tuner._measure), never a wait.  The
stroke path and 5-pin MIDI are not touched.  RAM only: the drive at the
start is put back at the end, on Stop, and on every abort (a stroke or
survey, the beam broken on this head, the head gone, the timeout).

The descent also ends early, as a normal finish ("near threshold"), when
the PT level comes within sweep.guard_frac of the threshold (only when the
first point shows the PT above it), so the beam is not lowered into a
break that would ring as a stroke.

Settings ("sweep"): points, settle_ms, timeout_s, guard_frac.
"""
from tune import TUNE_MA_BURSTS, ACTIVE_KINDS
from threshold_refresh import _int
from signal_bands import VREF   # the threshold PWM runs 0..VREF
from ring import FULL           # 16-bit duty full scale

SWEEP_POINTS = 16               # drives from max_duty down to min_duty, geometric
SWEEP_POINTS_MAX = 64           # cap (the point index is one 7-bit CC)
SWEEP_POINTS_MIN = 2
SWEEP_SETTLE_MS = 300           # after each drive change, before the first burst
SWEEP_TIMEOUT_S = 60            # the whole sweep (16 points take ~10 s)
SWEEP_GUARD_FRAC = 0.1          # stop when the PT is within this of the threshold
SWEEP_TICK_S = 0.05             # Aligner tick while a sweep runs (one burst per tick)
MS_PER_S = 1000
END_DONE = None                 # finished: every point
END_NEAR = "near threshold"     # finished early: the next step could break the beam


def sweep_settings(cfg):
    sec = cfg.get("sweep") if cfg is not None and hasattr(cfg, "get") else None
    sec = sec if isinstance(sec, dict) else {}
    n = _int(sec.get("points"), SWEEP_POINTS)
    if not SWEEP_POINTS_MIN <= n <= SWEEP_POINTS_MAX:
        n = SWEEP_POINTS
    settle = _int(sec.get("settle_ms"), SWEEP_SETTLE_MS)
    if settle < 0:
        settle = SWEEP_SETTLE_MS
    tmo = _int(sec.get("timeout_s"), SWEEP_TIMEOUT_S)
    if tmo <= 0:
        tmo = SWEEP_TIMEOUT_S
    g = sec.get("guard_frac", SWEEP_GUARD_FRAC)
    if isinstance(g, bool) or not isinstance(g, (int, float)) or not 0 <= g < 1:
        g = SWEEP_GUARD_FRAC
    return {"points": n, "settle_ms": settle, "timeout_ms": tmo * MS_PER_S, "guard_frac": g}


def sweep_duties(hi, lo, n):
    """n drives from hi down to lo, geometric (finer steps near the floor),
    distinct, hi first, lo last."""
    if lo < 1:
        lo = 1
    if hi <= lo or n < 2:
        return [hi]
    r = (lo / hi) ** (1.0 / (n - 1))
    out = []
    d = float(hi)
    for _ in range(n):
        v = int(d + 0.5)
        if not out or v < out[-1]:
            out.append(v)
        d *= r
    if out[-1] != lo:
        if out[-1] < lo:
            out[-1] = lo
        else:
            out.append(lo)
    return out


class Sweep:
    """One sweep on one head.  report(kind, ...) is Aligner's: ("point", i,
    drive, ma, pt, span) for each point, ("end", why) at the end (why None:
    every point; END_NEAR: finished early; else the abort reason)."""

    def __init__(self, host, ch, now, report, settings):
        self.host = host
        self.ch = ch
        self.report = report
        self.s = settings
        t = host.tune
        lim = t.settings["laser"]
        self.duties = sweep_duties(lim["max_duty"], lim["min_duty"], settings["points"])
        self.orig = int(host.bells[ch].laser.duty_cycle)
        self.started = now
        self.i = -1
        self.due = now
        self.k = 0
        self.pt_sum = 0.0
        self.span = 0.0
        self.low = False            # set from the sense pass (Aligner.edge)
        self.thr_v = None           # the threshold, volts, if the PT starts above it
        self.done = False
        t.sweep_ch = ch
        self._next(now)

    def _set(self, duty):
        self.host.bells[self.ch].laser.duty_cycle = duty

    def _next(self, now):
        self.i += 1
        self.k = 0
        self.pt_sum = 0.0
        self.span = 0.0
        self._set(self.duties[self.i])
        self.due = now + self.s["settle_ms"]

    def _abort_why(self, now):
        h = self.host
        if h.tower.tower != "inactive":
            return "tower not quiet"
        if h.survey_state.phase != h.tune._idle:
            return "survey running"
        if h.gate.kind[self.ch] not in ACTIVE_KINDS:
            return "head gone"
        if self.low or h._prev_held[self.ch]:
            return "beam broken"
        if now - self.started > self.s["timeout_ms"]:
            return "timeout"
        return None

    def stop(self, why):
        """Put the drive back and report the end (once)."""
        if self.done:
            return
        self.done = True
        try:
            self._set(self.orig)
        finally:
            t = self.host.tune
            t.sweep_ch = None
            t._bursts.pop(self.ch, None)
            self.report("end", why)

    def step(self, now):
        """One cooperative step.  -> True while running."""
        if self.done:
            return False
        why = self._abort_why(now)
        if why is not None:
            self.stop(why)
            return False
        if now - self.due < 0:
            return True
        t = self.host.tune
        ma = t._measure(self.ch)
        if ma is False:
            return True             # mux held this pass: try next tick
        pt = t.pt.get(self.ch)
        if pt is not None:
            self.pt_sum += pt[0]
            if pt[1] > self.span:
                self.span = pt[1]
        self.k += 1
        if self.k < TUNE_MA_BURSTS:
            return True
        drv = self.duties[self.i]
        pv = round(self.pt_sum / self.k, 3) if pt is not None else None
        self.report("point", self.i, drv, ma, pv, self.span)
        if self._near(pv):
            self.stop(END_NEAR)
            return False
        if self.i + 1 >= len(self.duties):
            self.stop(END_DONE)
            return False
        self._next(now)
        return True

    def _near(self, pv):
        thr = self.host.refresher.target.get(self.ch)
        if pv is None or thr is None:
            return False
        tv = thr * VREF / FULL
        if self.i == 0:
            if pv > tv:
                self.thr_v = tv     # the PT rises with light here: guard on
            return False
        return self.thr_v is not None and pv <= self.thr_v * (1 + self.s["guard_frac"])
