# threshold_refresh.py -- keeps the per-channel threshold hold caps right.
# Each outgoing threshold channel has a 100 nF hold cap on the connector
# board, after the output 4051.  Two separate processes keep them right
# (user's design, rates set by the user's scope droop tests -- those
# measurements are authoritative; the datasheet numbers below are only a
# cross-check):
#   * FAST REFRESH (ThresholdRefresher.step / run): walks the channels, takes
#     each channel's CURRENT target and re-writes it, on a fast clock.  It
#     never decides a value; it only fights droop.
#   * SLOW UPDATE (set_target / update_targets): infrequently
#     changes the target values, which drift over the day and night with
#     background light.
#
# Datasheet cross-check.  The 1 uF filter node (GP28 monitors it) refreshes
# whichever hold cap is selected:
#   charge share on select : node moves Ch/(C2+Ch) = 9.1 % of (Vhold - Vnode)
#   hold cap follows node  : tau = Ron*Ch ~ 50-100 us  (done in < 1 ms)
#   node recovers via 20 k : ~133 ms to 1 LSB after a full-scale difference
# Droop of a deselected hold cap: I/Ch.  LM393 input bias 25 nA typ / 250 nA
# max -> 0.25 / 2.5 V/s, one direction (PNP inputs source current, so the
# held threshold creeps UP [inferred]); 4051 off leakage ~nA and cap leakage
# are ~100x smaller.
import time

from mux_errors import ChannelBusy
from rc_timing import refresh_interval_s
from adc_avg import avg as _avg

try:
    import asyncio
except ImportError:
    asyncio = None

SHARE_FRAC = 100e-9 / (1e-6 + 100e-9)


class ThresholdRefresher:
    """Fast refresh of the per-channel hold caps, plus the slow target store.

    Fast refresh (step / run): round-robin over channels that have a target
    and are gate-allowed; select, re-write that channel's current target,
    dwell while the hold cap follows the node, release.  It never changes a
    target.
    Slow update (set_target / update_targets): the infrequent
    process that moves targets as background light drifts.  A changed
    target only takes effect at that channel's next refresh.

    One PWM serves every channel, so:
      * same target on every bell  -> dwell ~1 ms per channel (Ron*Ch*10),
        a full 8-channel cycle in ~8 ms;
      * different targets per bell -> each hop re-settles the RC
        (DrivePath.set, ~100-160 ms by the RC model), so the cycle is ~1 s.
        The user's scope tests decide whether that is acceptable.
    Never fights other users of the select lines: a busy tower is skipped.
    idle_only=True refreshes only while idle_fn() is true; the edge sensing
    on the sense GPIOs does not use the muxes, so running always
    (idle_only=False) is recommended.

    interval_s: the user's MEASURED max gap between refreshes of a channel
    (scope droop test).  If None, the datasheet estimate
    refresh_interval_s(tol_v, bias_a, hold_c) is used and
    interval_source == "datasheet"."""

    def __init__(self, tower, drive, bells, gate=None, hold_c=100e-9,
                 ron=1000.0, bias_a=25e-9, tol_v=0.005, clock=None,
                 interval_s=None, dwell_s=None):
        self.tower, self.drive, self.bells, self.gate = tower, drive, list(bells), gate
        self.dwell_s = 10.0 * ron * hold_c if dwell_s is None else float(dwell_s)
        self.datasheet_interval_s = refresh_interval_s(tol_v, bias_a, hold_c)
        if interval_s is None:
            self.interval_s = self.datasheet_interval_s
            self.interval_source = "datasheet"
        else:
            self.interval_s = float(interval_s)
            self.interval_source = "measured"
        self.clock = clock or time.monotonic
        self.target = {}         # slow process writes, fast loop reads
        self.target_set_at = {}  # when each target last changed
        self.target_updates = 0
        self.last = {}           # when each channel was last refreshed
        self._i = 0
        self.skipped_busy = 0
        self.idle_s = 0.01       # nothing to refresh: don't spin

    # ---- slow update process -----------------------------------------
    def set_target(self, bell, duty):
        """Slow process: change one channel's target.  Does not touch the
        hardware; the fast loop re-writes it at that channel's next turn."""
        duty = max(0, min(65535, int(duty)))
        if self.target.get(bell.index) != duty:
            self.target[bell.index] = duty
            self.target_set_at[bell.index] = self.clock()
            self.target_updates += 1
        return duty

    def update_targets(self, targets):
        """Slow process: {bell_index: duty} or a duty for every bell."""
        if isinstance(targets, dict):
            for b in self.bells:
                if b.index in targets:
                    self.set_target(b, targets[b.index])
        else:
            for b in self.bells:
                self.set_target(b, targets)

    # ---- fast refresh process ----------------------------------------
    def _eligible(self, b):
        if b.index not in self.target:
            return False
        return self.gate is None or self.gate.may_drive(b.index)

    def cycle_s(self):
        n = len([b for b in self.bells if self._eligible(b)])
        return n * self.dwell_s

    def step(self):
        """Fast refresh: re-write the next eligible bell's CURRENT target
        (whatever the slow process last set).  Returns its index, or None."""
        for _ in range(len(self.bells)):
            b = self.bells[self._i % len(self.bells)]
            self._i += 1
            if not self._eligible(b):
                continue
            try:
                sel = self.tower.select(b)
                sel.__enter__()
            except ChannelBusy:
                self.skipped_busy += 1
                return None
            try:
                want = self.target[b.index]
                if self.drive.duty != want:
                    self.drive.set(b.y, want)       # RC settle + GP28 verify
                self.tower.pins.sleep(self.dwell_s)  # hold cap follows node
            finally:
                sel.__exit__(None, None, None)
            self.last[b.index] = self.clock()
            return b.index
        return None

    def stale(self):
        now = self.clock()
        return [i for i in self.target
                if (self.gate is None or self.gate.may_drive(i))
                and now - self.last.get(i, -1e9) > self.interval_s]

    def droop_estimate(self, bell, n=16):
        """GP28 sees the charge-share step when a drooped hold cap is
        selected: droop ~= step / 9.1 %.  Read the settled node first (other
        channel selected), then select and read at once.  Counts, + = hold
        was above the node."""
        before = _avg(self.drive.back, n)
        with self.tower.select(bell):
            after = _avg(self.drive.back, n)
        return (after - before) / SHARE_FRAC

    def status(self):
        return {"targets": dict(self.target), "stale": self.stale(),
                "interval_s": self.interval_s,
                "interval_source": self.interval_source,
                "skipped_busy": self.skipped_busy}

    async def run(self, idle_fn=None, idle_only=False):
        """Fast refresh loop (just rewrites targets; set_target is the slow side)."""
        while True:
            if idle_only and idle_fn is not None and not idle_fn():
                await asyncio.sleep(0.05)
                continue
            i = self.step()
            await asyncio.sleep(0 if i is not None else self.idle_s)


def refresher_from_cfg(tower, bells, thr_cfg, gate=None, clock=None):
    """ThresholdRefresher from settings threshold_mux (hold_c, refresh{...}).
    refresh.interval_s = the user's measured value (null -> datasheet)."""
    r = thr_cfg.get("refresh") or {}
    drive = bells[0].drive if bells else None
    return ThresholdRefresher(tower, drive, bells, gate=gate,
                              hold_c=float(thr_cfg.get("hold_c", 100e-9)),
                              bias_a=float(r.get("bias_a", 25e-9)),
                              tol_v=float(r.get("tol_v", 0.005)),
                              dwell_s=r.get("dwell_s"),
                              interval_s=r.get("interval_s"), clock=clock)
