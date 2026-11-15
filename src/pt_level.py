"""
pt_level.py -- the raw phototransistor level of ONE channel, on its own ADC.

New-new heads (head 5 so far) bring the PT level out on the Orange/White
wire (RJ45 pin 1).  On the concentrator that wire of channel 1 goes straight
to GP27 (ADC1): no 4051, no filter.  Nothing else uses GP27 (tower_build
ignores a legacy threshold_mux.common of 27 and never claims it).

Settings (the state model), "pins": {"pt_level": {"gp": 27, "ch": 1}}; a
missing block means those defaults, false or null means not fitted.  ch is
1-based like the stroke events.

Not fitted on a head without the wire (heads 3 and 4 for now): GP27 then
reads whatever a floating Cat 5 conductor picks up.  This file cannot tell
the two apart (a pull test is unsafe on the RP2350: erratum E9 latches a
pulled-down input near 2 V, and the head's source impedance is unknown), so
every reading goes out with its spread across the burst and the page says
plainly that it is real only on a head with the Orange/White wire.

Reads are a short burst of ADC conversions (PT_N, well under 1 ms) on
GP27 alone (no mux, nothing a stroke uses): the alignment scope (also while
strokes are in progress), tune_get, and the heartbeat's "pt" (one reading
per heartbeat, so Sandbells can log day / night drift).
"""
from signal_bands import VREF

PT_LEVEL_GP = 27            # ADC1: the Orange/White wire of channel PT_LEVEL_CH
PT_LEVEL_CH = 1             # 1-based
PT_N = 16                   # conversions per reading (min, max and mean kept)
ADC_FULL = 65535            # AnalogIn.value full scale
PT_DP = 3                   # volts to this many decimals
ADC_GPS = (26, 27, 28, 29)
PT_ACTIVE_KINDS = ("head_new", "head_old")   # = tune.ACTIVE_KINDS: a head is there


def pt_settings(cfg, n_channels):
    """-> (gp, ch0) or None (not fitted).  ch0 is 0-based.  Bad values: None
    and a reason, so a typo never claims a wrong pin."""
    raw = None
    try:
        pins = cfg.get("pins") if cfg is not None and hasattr(cfg, "get") else None
    except Exception:
        pins = None
    if isinstance(pins, dict):
        if "pt_level" in pins:
            raw = pins.get("pt_level")
            if not raw:
                return None, "off in settings (pins.pt_level)"
        else:
            raw = {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        return None, "pins.pt_level is not an object"
    gp = raw.get("gp", PT_LEVEL_GP)
    ch = raw.get("ch", PT_LEVEL_CH)
    if isinstance(gp, bool) or not isinstance(gp, int) or gp not in ADC_GPS:
        return None, "pins.pt_level.gp %r is not an ADC pin" % (gp,)
    if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= n_channels:
        return None, "pins.pt_level.ch %r is not a channel" % (ch,)
    return (gp, ch - 1), None


def counts_to_v(counts):
    return round(counts * VREF / ADC_FULL, PT_DP)


class PtLevel:
    """One AnalogIn.  read() -> (volts mean, volts spread max-min)."""

    def __init__(self, adc, ch, gp):
        self.adc = adc
        self.ch = ch                # 0-based
        self.gp = gp
        self.last = None            # (v, span) of the last reading
        self.errors = 0

    def read(self):
        try:
            adc = self.adc
            lo = hi = s = adc.value
            for _ in range(PT_N - 1):
                v = adc.value
                s += v
                if v < lo:
                    lo = v
                elif v > hi:
                    hi = v
            self.last = (counts_to_v(s // PT_N), counts_to_v(hi - lo))
        except Exception:
            self.errors += 1
            self.last = None
        return self.last

    def for_ch(self, ch):
        """(v, span) for channel ch (0-based) or None: only the wired channel
        has a PT level."""
        if ch != self.ch:
            return None
        return self.read()

    def info(self):
        return {"ch": self.ch + 1, "gp": self.gp, "wiring": "direct, no mux",
                "valid": "only on a head with the Orange/White wire"}

    def close(self):
        try:
            self.adc.deinit()
        except Exception:
            pass


def open_pt_level(cfg, pins, n_channels, log=print):
    """PtLevel claimed through pins.analog_in, or None (off, bad settings,
    or the pin would not claim).  Never raises: the tower runs without it."""
    got, why = pt_settings(cfg, n_channels)
    if got is None:
        log("pt level: not fitted (%s)" % why)
        return None
    gp, ch = got
    try:
        adc = pins.analog_in(gp)
    except Exception as e:
        log("pt level: GP%d not claimed: %r" % (gp, e))
        return None
    log("pt level: ch %d Orange/White on GP%d (direct); real only on a head with that wire"
        % (ch + 1, gp))
    return PtLevel(adc, ch, gp)


def brief(pt, kind_of=None):
    """The heartbeat's "pt": [ch (1-based), volts, spread] read now, or None
    (not fitted, or the wired channel is not an active head).  kind_of(ch0)
    -> survey kind; None: do not check."""
    if pt is None:
        return None
    if kind_of is not None and kind_of(pt.ch) not in PT_ACTIVE_KINDS:
        return None
    got = pt.read()
    return None if got is None else [pt.ch + 1, got[0], got[1]]
