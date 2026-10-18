# pwm_slices.py -- RP2350A PWM slice map and the startup clash check. Leaf.
from mux_errors import PwmSliceError

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
