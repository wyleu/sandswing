# mux_select_draft.py -- DRAFT for sandswing (not committed anywhere)
#
# Tower / Bell split for two 4051s that share A/B/C select lines.
#
#   Tower  : owns A, B, C (and optional INH) DigitalInOut objects ONCE.
#            Exactly one channel is "current" = one bell.  Lock-protected.
#   Bell   : one channel (Y).  Its sense path (inward 4051, ADC on common)
#            and drive path (outward 4051, PWM/threshold on common) BORROW the
#            tower selection; they never touch A/B/C.
#
# Invariant (mentor, 2026-10-18): the inward and outward muxes always sit on
# the SAME channel.  So:
#   * a bell has ONE y, used by both muxes;
#   * while a bell holds the selection, selecting another bell raises
#     ChannelBusy (sync) or waits (async, optional timeout) -- never a silent
#     switch.
#   * the same bell may nest/re-enter (sense + drive inside one "with").
#
# CircuitPython-compatible: no typing, no dataclasses, no contextlib.
# Hardware modules (board, digitalio, time.sleep) are only touched through a
# pin factory, so plain CPython tests can inject fakes.

import time

try:
    import asyncio
except ImportError:  # board without the asyncio lib
    asyncio = None


class ChannelBusy(Exception):
    """Another bell holds the select lines."""


class NotSelected(Exception):
    """A bell path was used without holding the tower selection."""


class MuxConfigError(ValueError):
    pass


# --------------------------------------------------------------------------
# Pin factory (real hardware). Tests pass their own.
# --------------------------------------------------------------------------
class BoardPins:
    def output(self, gp, value=False):
        import board
        import digitalio
        p = digitalio.DigitalInOut(getattr(board, "GP%d" % int(gp)))
        p.switch_to_output(value=value)
        return p

    def analog_in(self, gp):
        import board
        import analogio
        return analogio.AnalogIn(getattr(board, "GP%d" % int(gp)))

    def pwm_out(self, gp, frequency=1000, duty_cycle=0):
        import board
        import pwmio
        return pwmio.PWMOut(getattr(board, "GP%d" % int(gp)),
                            frequency=frequency, duty_cycle=duty_cycle)

    def sleep(self, s):
        time.sleep(s)


# --------------------------------------------------------------------------
# Tower: the single owner of A/B/C
# --------------------------------------------------------------------------
class Tower:
    def __init__(self, a, b, c, inh=None, settle_s=0.001, pins=None, name="tower"):
        self.name = name
        self.pins = pins or BoardPins()
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
        self.selects = 0         # line changes (for tests / heartbeat stats)
        self._closed = False

    # ---- line driving -------------------------------------------------
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

    def _check_open(self):
        if self._closed:
            raise RuntimeError("Tower %s deinit" % self.name)

    def _take(self, y):
        """Non-blocking acquire. Returns True if lines changed."""
        self._check_open()
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

    # ---- public API ---------------------------------------------------
    def select(self, bell_or_y):
        """with tower.select(bell): ...   (sync; raises ChannelBusy)"""
        return _Selection(self, _channel_of(bell_or_y))

    def aselect(self, bell_or_y, timeout_s=None, poll_s=0.002):
        """async with tower.aselect(bell): ...  (waits; optional timeout)"""
        if asyncio is None:
            raise RuntimeError("asyncio not available")
        return _ASelection(self, _channel_of(bell_or_y), timeout_s, poll_s)

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
        _forget(self)


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


# --------------------------------------------------------------------------
# Mux paths (one per physical 4051). They own only their COMMON pin.
# --------------------------------------------------------------------------
class SensePath:
    """Inward (current-sense) 4051: COMMON -> GP26 AnalogIn."""
    def __init__(self, tower, common_gp, pins=None):
        self.tower = tower
        self.common_gp = int(common_gp)
        self.adc = (pins or tower.pins).analog_in(common_gp)

    def read(self, y, discard=1):
        if not self.tower.is_held_by(y):
            raise NotSelected("sense read ch %d without tower.select" % int(y))
        for _ in range(discard):
            _ = self.adc.value          # first sample after switch is stale
        return self.adc.value

    def deinit(self):
        try:
            self.adc.deinit()
        except Exception:
            pass


class ThresholdMismatch(Exception):
    """GP28 readback of the filtered threshold disagrees with the duty set."""


def rc2_tau_s(r1, c1, r2, c2):
    """Dominant time constant of the loaded 2-stage RC (R1-C1 then R2-C2).
    Poles of  a2 s^2 + a1 s + 1  with a2=R1C1R2C2, a1=R1C1+R2C2+R1C2.
    10k/100n + 10k/1u -> ~20.5 ms (vs 10 ms for the second stage alone)."""
    import math
    a2 = r1 * c1 * r2 * c2
    a1 = r1 * c1 + r2 * c2 + r1 * c2
    d = math.sqrt(a1 * a1 - 4.0 * a2)
    slow = (-a1 + d) / (2.0 * a2)
    return -1.0 / slow


def rc_settle_s(tau_s, step_frac, tol_frac, floor_s=0.0):
    """Time for a step of step_frac (of full scale) to come within tol_frac.
    Single dominant pole; add ~0.5 ms for the fast pole via floor_s."""
    import math
    step_frac = abs(step_frac)
    if step_frac <= tol_frac:
        return floor_s
    return tau_s * math.log(step_frac / tol_frac) + floor_s


class DrivePath:
    """Outward 4051: GP21 PWM -> 2-stage RC -> output 4051 COMMON (pin 3)
    -> selected bell's Orange wire (LM393 threshold).  GP28 also sits on the
    COMMON, so it reads back the threshold BEFORE the mux switch.

    Note: the 1 uF is upstream of the mux, so changing channel does not
    re-settle the RC; changing DUTY does (~170 ms for a full-scale step to
    1/4096).  A deselected head's Orange input is left floating unless the
    head has its own hold cap / bias (open hardware question)."""

    DEFAULT_RC = (10e3, 100e-9, 10e3, 1e-6)

    def __init__(self, tower, pwm_gp, frequency=20000, pins=None,
                 sense_gp=None, rc=None, tol_counts=1024, settle_counts=16, gain=1.0,
                 offset=0, verify=True):
        self.tower = tower
        p = pins or tower.pins
        self._sleep = p.sleep
        self.pwm = p.pwm_out(pwm_gp, frequency=frequency, duty_cycle=0)
        self.back = None if sense_gp is None else p.analog_in(sense_gp)
        r1, c1, r2, c2 = rc or self.DEFAULT_RC
        self.tau_s = rc2_tau_s(r1, c1, r2, c2)
        self.fast_s = 0.0005           # fast pole (~0.49 ms) margin
        self.tol_counts = int(tol_counts)      # readback acceptance window
        self.settle_counts = int(settle_counts)  # settle target (16 = 1 LSB of 12-bit)
        self.gain, self.offset = float(gain), int(offset)
        self.verify = bool(verify) and self.back is not None
        self.duty = 0
        self.last_settle_s = 0.0

    def settle_for(self, new_duty):
        step = (int(new_duty) - self.duty) / 65535.0
        return rc_settle_s(self.tau_s, step, self.settle_counts / 65535.0,
                           self.fast_s if step else 0.0)

    def expected(self, duty):
        return int(self.offset + self.gain * int(duty))

    def _apply(self, y, duty):
        if not self.tower.is_held_by(y):
            raise NotSelected("drive ch %d without tower.select" % int(y))
        duty = max(0, min(65535, int(duty)))
        wait = self.settle_for(duty)
        self.pwm.duty_cycle = duty
        self.duty = duty
        self.last_settle_s = wait
        return duty, wait

    def _check(self, y, duty):
        if not self.verify:
            return None
        got = self.readback(y)
        exp = self.expected(duty)
        if abs(got - exp) > self.tol_counts:
            raise ThresholdMismatch("ch %d duty=%d expected~%d got %d (tol %d)"
                                    % (int(y), duty, exp, got, self.tol_counts))
        return got

    def set(self, y, duty, wait=True):
        """Set threshold for the selected bell; wait the RC settle and verify
        on GP28.  Returns the readback (or None if verify is off)."""
        duty, t = self._apply(y, duty)
        if not wait:
            return None
        if t:
            self._sleep(t)
        return self._check(y, duty)

    async def aset(self, y, duty):
        duty, t = self._apply(y, duty)
        if t:
            await asyncio.sleep(t)
        return self._check(y, duty)

    def readback(self, y):
        if self.back is None:
            return None
        if not self.tower.is_held_by(y):
            raise NotSelected("readback ch %d without tower.select" % int(y))
        _ = self.back.value
        return self.back.value

    def calibrate(self, y, lo=8192, hi=57344):
        """Two-point gain/offset fit of GP28 vs duty (selected bell held)."""
        v = self.verify
        self.verify = False
        try:
            self.set(y, lo)
            r_lo = self.readback(y)
            self.set(y, hi)
            r_hi = self.readback(y)
        finally:
            self.verify = v
        self.gain = (r_hi - r_lo) / float(hi - lo)
        self.offset = int(r_lo - self.gain * lo)
        return self.gain, self.offset

    def off(self):
        self.pwm.duty_cycle = 0
        self.duty = 0

    def deinit(self):
        for o in (self.pwm, self.back):
            if o is not None:
                try:
                    o.deinit()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Bell: one channel, borrows both paths
# --------------------------------------------------------------------------
class Bell:
    def __init__(self, index, y, tower, sense=None, drive=None):
        self.index = int(index)
        self.y = int(y)
        self.tower = tower
        self.sense = sense
        self.drive = drive

    def read_sense(self):
        return self.sense.read(self.y)

    def set_drive(self, duty, wait=True):
        return self.drive.set(self.y, duty, wait)

    def readback(self):
        return self.drive.readback(self.y)

    def __repr__(self):
        return "Bell(%d, y=%d)" % (self.index, self.y)


# --------------------------------------------------------------------------
# RP2350 PWM slices (Pico 2 W = RP2350A, GP0..GP29 bonded out)
#
#   slice   = (gp >> 1) & 7      (8 slices on the QFN-60 part)
#   channel = "A" if gp is even else "B"
#
# Two pins on the same slice AND channel are the same PWM output: writing one
# pin's duty silently moves the other.  This bit sandswing once: the threshold
# PWM was on GP28 (slice 6 A) while bell 5's laser is GP12 (also 6 A).  The
# threshold now sits on GP21 (2 B).  Same slice, different channel is allowed
# (separate duty), but the counter TOP / divider -- the FREQUENCY -- belongs to
# the slice, so both channels must ask for the same frequency.
# GP32..47 exist only on RP2350B (QFN-80) and map to slices 8..11; the formula
# above is not valid there, so they are rejected here.
# --------------------------------------------------------------------------
class PwmSliceError(MuxConfigError):
    """Two PWM pins collide on one RP2350 slice/channel, or ask one slice
    for two frequencies."""


def pwm_slice(gp):
    """GP number -> (slice, "A"/"B") for the RP2350A (Pico 2 W)."""
    gp = int(gp)
    if not 0 <= gp <= 29:
        raise PwmSliceError("GP%d is not a Pico 2 W (RP2350A) GPIO" % gp)
    return (gp >> 1) & 7, ("A" if gp % 2 == 0 else "B")


def _pwm_items(pwm_pins):
    """{gp: hz} | [(gp, hz), ...] | [(gp, hz, role), ...] | [gp, ...]
    -> [(gp, hz or None, role)]."""
    if isinstance(pwm_pins, dict):
        items = list(pwm_pins.items())
    else:
        items = list(pwm_pins)
    out = []
    for it in items:
        if isinstance(it, (tuple, list)):
            gp = int(it[0])
            hz = None if len(it) < 2 or it[1] is None else int(it[1])
            role = it[2] if len(it) > 2 else "GP%d" % gp
        else:
            gp, hz, role = int(it), None, "GP%d" % int(it)
        out.append((gp, hz, role))
    return out


def check_pwm_slices(pwm_pins, freq_mismatch="raise"):
    """Startup check of every pin that will become a PWMOut.

    Raises PwmSliceError if two pins share a slice AND channel (or a pin is
    listed twice).  Same slice, different channel is fine, except that
    frequency is per slice: if the two ask for different frequencies this
    raises (freq_mismatch="raise", default) or returns a warning
    (freq_mismatch="warn").  Pins with hz None are not frequency-checked.

    Returns (table, warnings); table = {gp: (slice, channel, hz, role)}."""
    items = _pwm_items(pwm_pins)
    table, by_out, by_slice, warnings = {}, {}, {}, []
    for gp, hz, role in items:
        sl, ch = pwm_slice(gp)
        if gp in table:
            raise PwmSliceError("GP%d listed twice as PWM (%s, %s)"
                                % (gp, table[gp][3], role))
        other = by_out.get((sl, ch))
        if other is not None:
            raise PwmSliceError(
                "PWM clash: %s (GP%d) and %s (GP%d) are both RP2350 PWM slice "
                "%d channel %s -- one output, they would share a duty cycle. "
                "Move one pin (formula: slice=(gp>>1)&7, A=even, B=odd)."
                % (table[other][3], other, role, gp, sl, ch))
        by_out[(sl, ch)] = gp
        table[gp] = (sl, ch, hz, role)
        for ogp in by_slice.get(sl, []):
            ohz = table[ogp][2]
            if hz is not None and ohz is not None and hz != ohz:
                msg = ("PWM slice %d frequency conflict: %s (GP%d, %s) wants "
                       "%d Hz, %s (GP%d, %s) wants %d Hz; frequency is per "
                       "slice" % (sl, table[ogp][3], ogp, table[ogp][1], ohz,
                                  role, gp, ch, hz))
                if freq_mismatch == "warn":
                    warnings.append(msg)
                else:
                    raise PwmSliceError(msg)
        by_slice.setdefault(sl, []).append(gp)
    return table, warnings


def pwm_pins_from_cfg(pins_cfg, frequency=20000, laser_hz=1000):
    """The PWM pins a pins block will create: lasers + threshold PWM.
    laser frequency: pins.laser_pwm_hz (code uses 1 kHz today);
    threshold: threshold_mux.pwm_hz (20000, scope-tested)."""
    out = []
    lhz = int(pins_cfg.get("laser_pwm_hz", laser_hz))
    for i, gp in enumerate(pins_cfg.get("laser_pwm") or []):
        out.append((int(gp), lhz, "laser ch%d" % (i + 1)))
    thr = pins_cfg.get("threshold_mux") or {}
    if thr.get("pwm_gp") is not None:
        out.append((int(thr["pwm_gp"]), int(thr.get("pwm_hz", frequency)),
                    "threshold PWM"))
    return out


# --------------------------------------------------------------------------
# Registry + settings.json mapping
# --------------------------------------------------------------------------
_TOWERS = {}


def _forget(tower):
    for k in list(_TOWERS):
        if _TOWERS[k] is tower:
            del _TOWERS[k]


def get_tower(name, spec, pins=None):
    """One Tower per name. A second call with the same pins returns the same
    object; different pins under the same name, or a second name reusing any
    select pin, raises MuxConfigError (instead of 'pin in use' later)."""
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


def reset_registry():
    """Test helper / soft-restart helper: deinit every tower."""
    for t in list(_TOWERS.values()):
        t.holder = None
        t.depth = 0
        t.deinit()
    _TOWERS.clear()


def _group_for(mux_cfg, groups):
    """Mux entry -> (group_name, spec). New style: "select": "tower".
    Legacy style (current settings_example.json): inline a/b/c -> an
    implicit group keyed by the pin tuple, so identical a/b/c share."""
    name = mux_cfg.get("select")
    if name is not None:
        if name not in groups:
            raise MuxConfigError("unknown select group %r" % (name,))
        return name, groups[name]
    spec = {k: mux_cfg.get(k) for k in ("a", "b", "c", "inh")}
    if spec["a"] is None:
        raise MuxConfigError("mux has neither 'select' nor a/b/c")
    return "abc_%s_%s_%s" % (spec["a"], spec["b"], spec["c"]), spec


# Concentrator I/O (mentor, 2026-10-18): 8 channels, Y0..Y7 = ch 1..8 on
# both 4051s; ch 1-4 populated (connector board 1), 5-8 to the expansion
# connector.  Select GPs 18/19/20 are from settings_example.json [assumed].
DEFAULT_PINS = {
    "sense": [0, 1, 2, 3, 4, 5, 6, 7],          # Blue/White, digital in
    "laser_pwm": [8, 9, 10, 11, 12, 13, 14, 15],  # Green, digital/PWM out
    "neopixel": 16,
    "select_groups": {"tower": {"a": 18, "b": 19, "c": 20, "inh": None,
                                "settle_us": 1000}},
    "adc_mux": {"select": "tower", "common": 26, "y": [0, 1, 2, 3, 4, 5, 6, 7]},
    "threshold_mux": {"select": "tower", "pwm_gp": 21, "pwm_hz": 20000,
                      "sense_gp": 28, "y": [0, 1, 2, 3, 4, 5, 6, 7],
                      "rc": {"r1": 10e3, "c1": 100e-9, "r2": 10e3, "c2": 1e-6},
                      "hold_c": 100e-9},
    "connector_boards": DEFAULT_BOARDS if "DEFAULT_BOARDS" in globals() else None,
}


def build_from_pins(pins_cfg=None, pins=None, frequency=20000,
                    freq_mismatch="raise"):
    """pins_cfg = settings.json["pins"]. Returns (tower, [Bell, ...]).

    Enforces: both muxes resolve to the SAME Tower, give each bell the
    same Y on both sides, and (check_pwm_slices) no two PWM pins -- lasers
    and threshold -- share an RP2350 slice+channel or ask one slice for two
    frequencies.  frequency = threshold PWM Hz if threshold_mux.pwm_hz is
    absent (20000, scope-tested)."""
    if pins_cfg is None:
        pins_cfg = DEFAULT_PINS
    if "pins" in pins_cfg:                 # whole settings.json passed
        pins_cfg = pins_cfg["pins"]
    groups = pins_cfg.get("select_groups") or {}
    sense_cfg = pins_cfg.get("adc_mux") or {}
    drive_cfg = pins_cfg.get("threshold_mux") or {}
    # Before claiming anything: no two PWM pins on one slice+channel, one
    # frequency per slice (raises PwmSliceError).
    pwm_table, pwm_warn = check_pwm_slices(pwm_pins_from_cfg(pins_cfg, frequency),
                                           freq_mismatch)
    sname, sspec = _group_for(sense_cfg, groups)
    tower = get_tower(sname, sspec, pins)
    if drive_cfg:
        dname, dspec = _group_for(drive_cfg, groups)
        dtower = get_tower(dname, dspec, pins)
        if dtower is not tower:
            raise MuxConfigError("sense and drive muxes must share one select "
                                 "group (%r vs %r)" % (sname, dname))

    n = len(pins_cfg.get("sense") or [])
    sy = list(sense_cfg.get("y") or sense_cfg.get("laser_y") or range(8))
    dy = list(drive_cfg.get("y") or sy) if drive_cfg else sy
    for i in range(n):
        if sy[i] != dy[i]:
            raise MuxConfigError("bell %d: sense y=%d != drive y=%d (shared-"
                                 "channel invariant)" % (i, sy[i], dy[i]))

    tower.warnings = list(pwm_warn)
    tower.pwm_slices = pwm_table
    if drive_cfg.get("common") is not None:
        # GP27 is not used: the output 4051 COMMON is driven by the GP21
        # filter and monitored by GP28 (sense_gp).  Ignore, never claim.
        tower.warnings.append("threshold_mux.common=%r ignored (no pin on the "
                              "output COMMON except GP28 monitor)"
                              % (drive_cfg.get("common"),))
    sense = SensePath(tower, sense_cfg.get("common", 26), pins)
    drive = None
    if drive_cfg and drive_cfg.get("pwm_gp") is not None:
        rc = drive_cfg.get("rc")         # {"r1":10e3,"c1":100e-9,"r2":10e3,"c2":1e-6}
        rc = None if not rc else (rc["r1"], rc["c1"], rc["r2"], rc["c2"])
        drive = DrivePath(tower, drive_cfg["pwm_gp"],
                          int(drive_cfg.get("pwm_hz", frequency)), pins,
                          sense_gp=drive_cfg.get("sense_gp"), rc=rc,
                          tol_counts=int(drive_cfg.get("readback_tol", 1024)))
    bells = [Bell(i, sy[i], tower, sense, drive) for i in range(n)]
    return tower, bells


# --------------------------------------------------------------------------
# Laser presence (secondary; the 15 R exists mainly for loopback testing)
# --------------------------------------------------------------------------
SENSE_OHMS = 15.0
VREF = 3.3
COUNTS_PER_MA = 65535.0 * SENSE_OHMS * 1e-3 / VREF   # ~297.9 counts/mA (16-bit scale)
FLOOR_COUNTS = 900        # README: empty port / resting head read 500-900


def counts_to_ma(counts):
    return counts / COUNTS_PER_MA


def classify_laser(dark, on, expected_ma=5.0, ref_ma=None, floor=FLOOR_COUNTS,
                   max_ma=60.0, drift_frac=0.35):
    """dark = GP26 counts with laser duty 0, on = counts at duty 65535 (DC, so
    no PWM chopping); both averaged.  Returns (status, i_on_ma, i_dark_ma).

    Bands are relative to expected_ma (mentor: ~5 mA modules -> ~1490 counts)
    or, better, to ref_ma measured per head at commissioning:
      stuck_on : dark > floor + 0.4*expected   (current with the drive off)
      open     : step < 0.4*expected
      over     : step > max_ma
      drift    : |step - ref| > drift_frac*ref
    NOTE: the README bench step (~+8000 counts, ~27 mA) does not match 5 mA;
    set expected_ma / ref_ma from a commissioning run."""
    i_dark = counts_to_ma(dark)
    i_on = counts_to_ma(on - dark)
    if dark > floor + 0.4 * expected_ma * COUNTS_PER_MA:
        return "stuck_on", i_on, i_dark
    if i_on < 0.4 * expected_ma:
        return "open", i_on, i_dark
    if i_on > max_ma:
        return "over", i_on, i_dark
    if ref_ma and abs(i_on - ref_ma) > drift_frac * ref_ma:
        return "drift", i_on, i_dark
    return "ok", i_on, i_dark


def _avg(adc, n):
    s = 0
    for _ in range(n):
        s += adc.value
    return s // n


def measure_laser(tower, bell, laser_pwm, adc, settle=None, n=16):
    """With the bell selected: dark average, laser at full duty (DC), average.
    Leaves the laser OFF."""
    sleep = tower.pins.sleep
    settle = 0.05 if settle is None else settle   # matches head_present.SETTLE_S
    with tower.select(bell):
        laser_pwm.duty_cycle = 0
        sleep(settle)
        dark = _avg(adc, n)
        laser_pwm.duty_cycle = 65535
        sleep(settle)
        on = _avg(adc, n)
        laser_pwm.duty_cycle = 0
    return dark, on


# --------------------------------------------------------------------------
# Loopback self-test (per channel, boot / daily, tower inactive only)
#
# Loopback plug, per channel:
#   path A  Green (laser PWM GPn)          -> Blue/White (sense GP0..)
#   path B  Green/White (inward 4051 Yn)   <- Orange (outward 4051 Yn)
# Path B end to end: GP21 PWM -> RC -> outward COMMON (GP28) -> Yn -> Orange
#   -> plug -> Green/White -> inward Yn -> inward COMMON (100 k to GND) -> GP26
# Expected (model H, 15 R inside the head, so not in the plug path; Ron ~0.5-1 k):
#   loaded  : GP28 ~0.835*duty, GP26 ~0.82-0.83*duty, GP26/GP28 ~0.98-0.99
#   no plug : GP28 ~1.0*duty (unloaded), GP26 ~ floor (500-900)
# --------------------------------------------------------------------------
LB_RATIO_MIN = 0.93           # GP26/GP28 when looped (Ron up to ~3 k still passes)
LB_LOAD_MIN, LB_LOAD_MAX = 0.75, 0.92   # GP28 / unloaded-expected when looped
UNLOADED_MIN = 0.95           # GP28 / expected with no plug
DAC_MAX_RESID = 1311          # 2 % FS: RP2xxx ADC ENOB ~9 bit [inferred]
SWEEP_TOL = 0.12              # fraction-high vs duty, path A sweep


def _fit(xs, ys):
    n = float(len(xs))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    g = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    o = my - g * mx
    return g, o, max(abs(y - (g * x + o)) for x, y in zip(xs, ys))


class LoopbackSelfTest:
    """Built on Tower/Bell.  Each bell needs .drive (DrivePath with GP28
    readback), .sense (SensePath on GP26); for path A also bell.laser
    (PWMOut) and bell.sense_pin (DigitalInOut input).  All steps hold the
    tower on that bell, so the shared-channel invariant is respected."""

    def __init__(self, tower, n_avg=16, sweep_samples=200):
        self.tower = tower
        self.n_avg = n_avg
        self.sweep_samples = sweep_samples

    def _sleep(self, s):
        self.tower.pins.sleep(s)

    def _thr(self, bell, duty):
        d = bell.drive
        v = d.verify
        d.verify = False                  # loaded nodes read low on purpose
        try:
            d.set(bell.y, duty)
        finally:
            d.verify = v
        # a plug changes the load: allow a full RC settle after selection
        self._sleep(rc_settle_s(d.tau_s, 0.2, d.settle_counts / 65535.0))
        return _avg(d.back, self.n_avg), _avg(bell.sense.adc, self.n_avg)

    def dac_linearity(self, bell, steps=8):
        """GP21 -> RC -> GP28.  Run on a channel WITHOUT a plug (unloaded)."""
        xs, ys = [], []
        with self.tower.select(bell):
            for k in range(steps + 1):
                duty = int(65535 * k / steps)
                r28, _ = self._thr(bell, duty)
                xs.append(duty)
                ys.append(r28)
            bell.drive.off()
        g, o, resid = _fit(xs, ys)
        ok = resid <= DAC_MAX_RESID and g > 0.9
        return {"test": "dac_linearity", "ch": bell.index, "ok": ok,
                "gain": g, "offset": o, "max_resid": resid}

    def threshold_loopback(self, bell, duty=32768):
        """Path B.  Returns plug=True/False and ok (consistent reading)."""
        with self.tower.select(bell):
            r28, r26 = self._thr(bell, duty)
            bell.drive.off()
        exp = bell.drive.expected(duty)
        load = r28 / float(exp) if exp else 0.0
        ratio = r26 / float(r28) if r28 else 0.0
        if ratio >= LB_RATIO_MIN and LB_LOAD_MIN <= load <= LB_LOAD_MAX:
            plug, ok, why = True, True, "looped"
        elif load >= UNLOADED_MIN and r26 < 0.25 * r28:
            plug, ok, why = False, True, "no plug (head or empty)"
        else:
            plug, ok, why = None, False, "inconsistent: check switch/cable/Ron"
        return {"test": "threshold_loopback", "ch": bell.index, "ok": ok,
                "plug": plug, "why": why, "gp28": r28, "gp26": r26,
                "load": load, "ratio": ratio}

    def digital_loopback(self, bell, settle=0.01):
        """Path A on/off: laser duty 0 -> sense False, 65535 -> True."""
        got = []
        with self.tower.select(bell):
            for duty in (0, 65535, 0):
                bell.laser.duty_cycle = duty
                self._sleep(settle)
                got.append(bool(bell.sense_pin.value))
            bell.laser.duty_cycle = 0
        ok = got == [False, True, False]
        plug = (True if ok else None) if any(got) else False
        return {"test": "digital_loopback", "ch": bell.index, "ok": ok,
                "plug": plug, "seen": got}

    def duty_sweep(self, bell, duties=(0, 16384, 32768, 49152, 65535)):
        """Path A 'analog': fraction of samples high vs PWM duty.  Valid only
        if the connector-board filter corner is well above the 1 kHz PWM;
        with heavy 'Dot A' filtering expect a step instead (flagged)."""
        frac = []
        with self.tower.select(bell):
            for duty in duties:
                bell.laser.duty_cycle = duty
                self._sleep(0.01)
                hi = 0
                for _ in range(self.sweep_samples):
                    hi += 1 if bell.sense_pin.value else 0
                    self._sleep(0.000137)   # not a multiple of the PWM period
                frac.append(hi / float(self.sweep_samples))
            bell.laser.duty_cycle = 0
        err = max(abs(f - d / 65535.0) for f, d in zip(frac, duties))
        mono = all(frac[i] <= frac[i + 1] + 0.02 for i in range(len(frac) - 1))
        return {"test": "duty_sweep", "ch": bell.index, "ok": err <= SWEEP_TOL,
                "monotonic": mono, "frac": frac, "max_err": err}

    def run(self, bells, dac_bell=None):
        """Boot / daily self-check.  Returns {"ok": bool, "channels": {...}}."""
        report = {"channels": {}}
        for b in bells:
            r = {"threshold": self.threshold_loopback(b)}
            if getattr(b, "laser", None) is not None and \
                    getattr(b, "sense_pin", None) is not None and r["threshold"]["plug"]:
                r["digital"] = self.digital_loopback(b)
                r["sweep"] = self.duty_sweep(b)
            report["channels"][b.index] = r
        free = [b for b in bells if report["channels"][b.index]["threshold"]["plug"] is False]
        db = dac_bell or (free[0] if free else None)
        report["dac"] = self.dac_linearity(db) if db is not None else \
            {"test": "dac_linearity", "ok": None, "why": "no unloaded channel"}
        oks = [t["ok"] for r in report["channels"].values() for t in r.values()]
        report["ok"] = all(oks) and report["dac"]["ok"] is not False
        return report


# --------------------------------------------------------------------------
# Socket classification (8P8C keystone per channel)
#
# Measurements, all with tower.select(bell) held:
#   l0    GP28 / FS at threshold duty 0            (Orange shorted to +V?)
#   l50   GP28 / expected-unloaded at duty 50 %    (load on Orange)
#   r50   GP26 / GP28 at duty 50 %, laser off      (Orange <-> Green/White strap)
#   dark  GP26 counts, laser off, threshold 0
#   di_ma GP26 step laser 0 -> 65535 (DC), in mA   (laser present)
#   x     |GP28 change| when laser toggled / FS    (cross-coupled pairs)
#   follow sense follows laser 0/full/0             (Green <-> Blue/White strap)
#   pull_follow sense follows internal pull UP/DOWN (nothing drives Blue/White)
#   thr_toggle  sense changes as Orange swept       (LM393 in circuit)
# --------------------------------------------------------------------------
def classify_socket(m, expected_ma=5.0, floor=FLOOR_COUNTS):
    """Return (kind, reasons).  kind in: loopback, head_new, head_old, empty,
    fault.  Signature thresholds are first guesses, to be tuned on the bench."""
    why = []
    laser = m.get("di_ma", 0.0) >= 0.4 * expected_ma
    strap_b = m.get("r50", 0.0) >= LB_RATIO_MIN
    strap_a = bool(m.get("follow"))
    l50 = m.get("l50", 1.0)

    # hard faults first
    if m.get("l0", 0.0) > 0.10:
        return "fault", ["Orange pulled up at duty 0: short to +V"]
    if l50 < 0.20:
        return "fault", ["Orange into ~0 ohm: short to GND or crossed onto "
                         "Green/White (15 R) in a head cable"]
    if m.get("dark", 0) > floor + 0.4 * expected_ma * COUNTS_PER_MA:
        return "fault", ["GP26 high with laser off: Green/White to +V, or "
                         "laser drive stuck on"]
    if m.get("x", 0.0) > 0.02:
        return "fault", ["GP28 moves with the laser: Orange crossed with a "
                         "laser conductor"]
    if m.get("di_ma", 0.0) > 60.0:
        return "fault", ["huge 'laser' step: pins 6-3 DC-shorted (Ethernet "
                         "magnetics?), Green shorted to Green/White, or "
                         "laser short. Drive OFF"]
    if strap_b and laser:
        return "fault", ["Orange strapped to Green/White AND laser current: "
                         "inter-pair short in a head cable"]
    # loopback plug
    if strap_a and strap_b and LB_LOAD_MIN <= l50 <= LB_LOAD_MAX:
        return "loopback", ["both straps seen"]
    if strap_a != strap_b and not laser:
        return "fault", ["partial loopback (%s strap only): open conductor "
                         "or mis-strapped plug" % ("A" if strap_a else "B")]
    if 0.20 <= l50 < 0.70:
        return "fault", ["Orange into ~10 k: Orange swapped with Blue/White "
                         "(old-head emitter) or high series resistance"]
    # heads
    if laser:
        if m.get("thr_toggle"):
            return "head_new", ["laser current, sense follows Orange sweep"]
        if not m.get("pull_follow", True):
            return "head_old", ["laser current, sense driven (emitter 10 k), "
                                "Orange not used"]
        return "fault", ["laser current but nothing drives Blue/White: open "
                         "sense conductor, unpowered PT/LM393, or LM393 "
                         "without pull-up"]
    if (not m.get("pull_follow", True)) or m.get("thr_toggle"):
        return "fault", ["head-like sense but no laser current: laser open, "
                         "Green (pin 6) open, or laser 0 V return (pin 7) "
                         "open"]
    if m.get("pull_follow", True) and l50 >= UNLOADED_MIN:
        return "empty", ["nothing on any line (no plug; fit a loopback blank)"]
    why.append("unrecognised signature %r" % (m,))
    return "fault", why


class SocketSurvey(LoopbackSelfTest):
    """Collects the measurements above for one bell, then classifies."""

    def __init__(self, tower, pull_up=None, pull_down=None, **kw):
        LoopbackSelfTest.__init__(self, tower, **kw)
        if pull_up is None:
            import digitalio
            pull_up, pull_down = digitalio.Pull.UP, digitalio.Pull.DOWN
        self.pull_up, self.pull_down = pull_up, pull_down

    def measure(self, bell, sweep=(0, 16384, 32768, 49152, 65535)):
        d, adc = bell.drive, bell.sense.adc
        m = {}
        with self.tower.select(bell):
            bell.laser.duty_cycle = 0
            r28, r26 = self._thr(bell, 0)
            m["l0"] = r28 / 65535.0
            m["dark"] = r26
            bell.laser.duty_cycle = 65535
            self._sleep(0.05)
            on = _avg(adc, self.n_avg)
            r28_on = _avg(d.back, self.n_avg)
            bell.laser.duty_cycle = 0
            self._sleep(0.05)
            m["di_ma"] = counts_to_ma(on - r26)
            m["x"] = abs(r28_on - r28) / 65535.0
            r28, r26 = self._thr(bell, 32768)
            exp = d.expected(32768)
            m["l50"] = r28 / float(exp) if exp else 0.0
            m["r50"] = r26 / float(r28) if r28 else 0.0
            seen = []
            for duty in sweep:
                self._thr(bell, duty)
                seen.append(bool(bell.sense_pin.value))
            m["thr_toggle"] = len(set(seen)) > 1
            d.off()
        m["follow"] = self.digital_loopback(bell)["ok"]
        with self.tower.select(bell):
            p = bell.sense_pin
            old = p.pull
            p.pull = self.pull_up
            self._sleep(0.002)
            up = bool(p.value)
            p.pull = self.pull_down
            self._sleep(0.002)
            dn = bool(p.value)
            p.pull = old
        m["pull_follow"] = up and not dn
        return m

    def classify(self, bell, expected_ma=5.0):
        m = self.measure(bell)
        kind, why = classify_socket(m, expected_ma)
        return {"ch": bell.index, "kind": kind, "why": why, "m": m}


# --------------------------------------------------------------------------
# Drive gate: never drive a channel whose socket is not positively known.
# --------------------------------------------------------------------------
DRIVE_OK = ("loopback", "head_new", "head_old")


class ChannelGate:
    """Per-channel permission for laser PWM and threshold drive.
    unknown/empty/fault -> drive must stay at 0 (except the classifier's own
    short, low-energy probes).  A fault is latched until clear()."""

    def __init__(self, n):
        self.kind = ["unknown"] * n
        self.latched = [False] * n

    def set_kind(self, ch, kind):
        if self.latched[ch] and kind != "fault":
            return self.kind[ch]           # stays fault until cleared
        self.kind[ch] = kind
        if kind == "fault":
            self.latched[ch] = True
        return kind

    def clear(self, ch):
        self.latched[ch] = False
        self.kind[ch] = "unknown"

    def may_drive(self, ch):
        return self.kind[ch] in DRIVE_OK

    def enforce(self, bells):
        """Call every loop / heartbeat: force drive off where not allowed."""
        for b in bells:
            if not self.may_drive(b.index):
                if getattr(b, "laser", None) is not None:
                    b.laser.duty_cycle = 0


# --------------------------------------------------------------------------
# Threshold sample-and-hold refresh
#
# Each outgoing threshold channel has a 100 nF hold cap on the connector
# board, after the output 4051.  Two separate processes keep them right
# (user's design, rates set by the user's scope droop tests -- those
# measurements are authoritative; the datasheet numbers below are only a
# cross-check):
#   * FAST REFRESH (ThresholdRefresher.step / run): walks the channels, takes
#     each channel's CURRENT target and re-writes it, on a fast clock.  It
#     never decides a value; it only fights droop.
#   * SLOW UPDATE (set_target / update_targets / run_updates): infrequently
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
# --------------------------------------------------------------------------
SHARE_FRAC = 100e-9 / (1e-6 + 100e-9)


def refresh_interval_s(tol_v=0.005, bias_a=25e-9, hold_c=100e-9):
    """Datasheet estimate: longest gap between refreshes of one channel for
    droop <= tol_v.  Cross-check only; prefer the measured interval."""
    return hold_c * tol_v / bias_a


class ThresholdRefresher:
    """Fast refresh of the per-channel hold caps, plus the slow target store.

    Fast refresh (step / run): round-robin over channels that have a target
    and are gate-allowed; select, re-write that channel's current target,
    dwell while the hold cap follows the node, release.  It never changes a
    target.
    Slow update (set_target / update_targets / run_updates): the infrequent
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

    async def run_updates(self, target_fn, period_s=60.0):
        """Slow update loop: every period_s, target_fn() -> targets (dict or
        one duty), e.g. from ambient light.  Runs alongside run()."""
        while True:
            t = target_fn()
            if t is not None:
                self.update_targets(t)
            await asyncio.sleep(period_s)

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

    async def run(self, idle_fn=None, idle_only=False):
        """Fast refresh loop (just rewrites targets; see run_updates)."""
        while True:
            if idle_only and idle_fn is not None and not idle_fn():
                await asyncio.sleep(0.05)
                continue
            self.step()
            await asyncio.sleep(0)


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


# --------------------------------------------------------------------------
# Connector boards: ch 1-4 on board 1 (fitted); ch 5-8 are on the Pico board
# but run to an expansion connector for a future board 2.
# --------------------------------------------------------------------------
DEFAULT_BOARDS = [
    {"name": "conn1", "channels": [0, 1, 2, 3], "present": True},
    {"name": "conn2", "channels": [4, 5, 6, 7], "present": False},  # None = auto
]


def board_of(ch, boards=DEFAULT_BOARDS):
    for b in boards:
        if ch in b["channels"]:
            return b
    return None


def apply_boards(results, boards=DEFAULT_BOARDS):
    """results: {ch: kind}.  Channels on a board configured absent become
    'no_board'.  present=None (auto): if every channel of that board reads
    'empty' (nothing on any line), the board is taken as absent -> 'no_board'
    for all of them, not 'empty' or 'fault'."""
    out = dict(results)
    for b in boards:
        chans = [c for c in b["channels"] if c in out]
        if b.get("present") is False:
            for c in chans:
                out[c] = "no_board"
        elif b.get("present") is None and chans and \
                all(out[c] == "empty" for c in chans):
            for c in chans:
                out[c] = "no_board"
    return out


def survey_all(survey, bells, gate, boards=DEFAULT_BOARDS, expected_ma=5.0):
    """Classify every bell; never probe a channel whose board is configured
    absent (no drive at all).  Updates the gate.  Returns {ch: report}."""
    rep = {}
    for b in bells:
        brd = board_of(b.index, boards)
        if brd is not None and brd.get("present") is False:
            rep[b.index] = {"ch": b.index, "kind": "no_board",
                            "why": ["%s configured absent" % brd["name"]]}
            continue
        rep[b.index] = survey.classify(b, expected_ma)
    kinds = apply_boards({c: r["kind"] for c, r in rep.items()}, boards)
    for c, k in kinds.items():
        if k == "no_board" and rep[c]["kind"] != "no_board":
            rep[c]["why"] = ["no connector board detected (all channels empty)"]
        rep[c]["kind"] = k
        gate.set_kind(c, k)
    return rep


# --------------------------------------------------------------------------
# Front panel: 6 x WS2813 on GP16 (GRB). index 0 = left status,
# 1..4 = under sockets 1..4 (channel index + 1), 5 = right status.
# --------------------------------------------------------------------------
PANEL_N = 6
LED_KIND = {
    "loopback": (0, 0, 24),      # blue: known blank / plug
    "head_new": (0, 32, 0),      # green
    "head_old": (16, 32, 0),     # yellow-green
    "empty":    (24, 12, 0),     # amber: nothing fitted, fit a blank
    "no_board": (0, 0, 0),       # off (only for 5-8; they have no panel LED)
    "fault":    (48, 0, 0),      # red (blink in firmware)
    "unknown":  (8, 8, 8),       # dim white: classifying, drive OFF
}
LED_LINK = {"ws_client": (0, 32, 0), "wifi_only": (0, 0, 32),
            "no_wifi": (32, 12, 0), "error": (48, 0, 0)}
LED_ACTIVITY = {"heartbeat": (8, 8, 8), "ringing": (0, 32, 0),
                "ringing_down": (32, 16, 0), "selftest": (0, 24, 24),
                "watchdog_reset": (48, 0, 48)}


def panel_pixels(kinds, link="no_wifi", activity="heartbeat", beat_on=True):
    """kinds: {ch: kind} for ch 0..3 -> list of 6 RGB tuples."""
    px = [(0, 0, 0)] * PANEL_N
    px[0] = LED_LINK.get(link, LED_LINK["error"])
    for ch in range(4):
        px[ch + 1] = LED_KIND.get(kinds.get(ch, "unknown"), LED_KIND["unknown"])
    a = LED_ACTIVITY.get(activity, LED_ACTIVITY["heartbeat"])
    px[5] = a if (beat_on or activity != "heartbeat") else (0, 0, 0)
    return px


DEFAULT_PINS["connector_boards"] = DEFAULT_BOARDS
POPULATED = [c for b in DEFAULT_BOARDS if b.get("present") for c in b["channels"]]
