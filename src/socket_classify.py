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
                          UNLOADED_MIN, TRIGGER_LOAD_MIN)


def _f(x, nd=2):
    """Short number for why texts (32-bit floats on the Pico)."""
    try:
        return ("%." + str(nd) + "f") % x
    except (TypeError, ValueError):
        return str(x)


def classify_socket(m, expected_ma=5.0, floor=FLOOR_COUNTS):
    """Return (kind, reasons).  kind in: loopback, trigger, head_new,
    head_old, empty, fault.  Signature thresholds are first guesses, to be
    tuned on the bench.  reasons[0] is one readable line that ends with the
    measurement(s) that decided it, e.g. "(l50 0.12 < 0.20)"."""
    why = []
    di = m.get("di_ma", 0.0)
    laser_min = 0.4 * expected_ma
    laser = di >= laser_min
    r50 = m.get("r50", 0.0)
    strap_b = r50 >= LB_RATIO_MIN
    strap_a = bool(m.get("follow"))
    l50 = m.get("l50", 1.0)
    l0 = m.get("l0", 0.0)
    dark = m.get("dark", 0)
    dark_max = floor + laser_min * COUNTS_PER_MA
    x = m.get("x", 0.0)

    # hard faults first
    if l0 > 0.10:
        return "fault", ["Orange pulled up at duty 0: short to +V "
                         "(l0 %s > 0.10)" % _f(l0)]
    if l50 < 0.20:
        return "fault", ["Orange shorted to ground or crossed onto "
                         "Green/White (15 R) in a head cable "
                         "(l50 %s < 0.20)" % _f(l50)]
    if dark > dark_max:
        return "fault", ["GP26 high with laser off: Green/White to +V, or "
                         "laser drive stuck on (dark %d > %d)"
                         % (int(dark), int(dark_max))]
    if x > 0.02:
        return "fault", ["GP28 moves with the laser: Orange crossed with a "
                         "laser conductor (x %s > 0.02)" % _f(x, 3)]
    if di > 60.0:
        return "fault", ["huge 'laser' step: pins 6-3 DC-shorted (Ethernet "
                         "magnetics?), Green shorted to Green/White, or "
                         "laser short. Drive OFF (%s mA > 60)" % _f(di, 1)]
    if strap_b and laser:
        return "fault", ["Orange strapped to Green/White AND laser current: "
                         "inter-pair short in a head cable (r50 %s, %s mA)"
                         % (_f(r50), _f(di, 1))]
    # loopback plug
    if strap_a and strap_b:
        return "loopback", ["both straps seen (r50 %s, sense follows laser)"
                            % _f(r50)]
    # trigger plug (test switch): Orange <-> Green/White strapped, plus a
    # push switch on Blue/White (to 0 V, or to Green, which run mode holds
    # at duty 0 = low).  No laser current, sense does not follow the laser,
    # Orange loaded like a loopback.  Blue/White idles high (switch open) or
    # sits low whatever the laser does (switch to 0 V held through the
    # survey).  Before the partial-strap fault and the 10 k band.  A switch
    # to Green held through the survey reads "both straps" above: loopback
    # until it is released (the plug-change rule then resurveys).
    up = bool(m.get("sense_hi")) or bool(m.get("pull_follow", True))
    if strap_b and not strap_a and not laser and l50 >= TRIGGER_LOAD_MIN:
        vals = "(r50 %s, l50 %s)" % (_f(r50), _f(l50))
        if up:
            return "trigger", ["trigger plug (test switch): Orange-Green/White "
                               "link, switch open (Blue/White idles high); "
                               "press it for a test stroke " + vals]
        return "trigger", ["trigger plug (test switch): Orange-Green/White "
                           "link, switch held during the survey (Blue/White "
                           "low); release, then press for a test stroke " + vals]
    if strap_a != strap_b and not laser:
        return "fault", ["partial loopback (%s strap only): open conductor "
                         "or mis-strapped plug (r50 %s, follow %s)"
                         % ("A" if strap_a else "B", _f(r50), strap_a)]
    if 0.20 <= l50 < 0.70:
        return "fault", ["Orange into ~10 k: Orange swapped with Blue/White "
                         "(old-head emitter) or high series resistance "
                         "(l50 %s in 0.20-0.70)" % _f(l50)]
    # heads
    if not laser and not strap_a and not strap_b and m.get("sense_hi"):
        return "empty", ["socket open: sense held high by board pull-up, "
                         "no return on Green/White, no laser current "
                         "(%s mA)" % _f(di, 1)]
    if laser:
        if m.get("thr_toggle"):
            return "head_new", ["laser current, sense follows Orange sweep "
                                "(%s mA)" % _f(di, 1)]
        if not m.get("pull_follow", True):
            return "head_old", ["laser current, sense driven (emitter 10 k), "
                                "Orange not used (%s mA)" % _f(di, 1)]
        return "fault", ["laser current but nothing drives Blue/White: open "
                         "sense conductor, unpowered PT/LM393, or LM393 "
                         "without pull-up (%s mA, sense follows pulls)"
                         % _f(di, 1)]
    if (not m.get("pull_follow", True)) or m.get("thr_toggle"):
        return "fault", ["head-like sense but no laser current: laser open, "
                         "Green (pin 6) open, or laser 0 V return (pin 7) "
                         "open (%s mA < %s)" % (_f(di, 1), _f(laser_min, 1))]
    if m.get("pull_follow", True) and l50 >= UNLOADED_MIN:
        return "empty", ["nothing on any line (no plug; fit a loopback blank)"]
    why.append("unrecognised signature (l0 %s l50 %s r50 %s %s mA x %s)"
               % (_f(l0), _f(l50), _f(r50), _f(di, 1), _f(x, 3)))
    return "fault", why
