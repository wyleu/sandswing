# socket_classify.py -- measurements -> socket kind.  Pure function.
#
# Measurements (taken by socket_survey.SocketSurvey, tower held on the bell):
#   l0    GP28 / FS at threshold duty 0            (Orange shorted to +V?)
#   l50   GP28 / expected-unloaded at duty 50 %    (load on Orange)
#   r50   GP26 / GP28 at duty 50 %, laser off      (Orange <-> Green/White strap)
#   dark  GP26 counts, laser off, threshold 0
#   di_ma GP26 step laser 0 -> 65535 (DC), in mA   (laser present)
#   x     |GP28 change| when laser toggled / FS    (cross-coupled pairs)
#   follow sense follows laser 0/full/0             (Green <-> Blue/White strap)
#   pull_follow sense follows internal pull UP/DOWN (nothing drives Blue/White)
#   thr_toggle  sense changes as Orange swept       (LM393 in circuit)
#
# TODO(spec section 4, C1-C8): this is the draft's fault-first order.  The
# spec wants Empty-first evidence steps (C5 loopback waggle/ramp, C6 head
# exists, C7 rest hint, C8 "head_pending"), with old/new decided only in the
# reflector stage (R2).  See sandswing_code_changes.md section A.
from signal_bands import (COUNTS_PER_MA, FLOOR_COUNTS, LB_RATIO_MIN,
                          LB_LOAD_MIN, LB_LOAD_MAX, UNLOADED_MIN)

KINDS = ("loopback", "head_new", "head_old", "empty", "fault")


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
