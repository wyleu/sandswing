"""
tune.py -- live (RAM only) threshold and laser-duty adjustments.

Settings (defaults below) are the state model; nothing here writes
settings.json.  A reboot returns each head to the seeded threshold
(threshold.default_duty on a new head) and laser duty FULL.

Commands arrive as one JSON line on the USB data port (usb_cdc.data,
the same port as serial_status).  Acks go back on that port.

    {"cmd":"tune","ch":1,"thr":32768}       set threshold duty (absolute)
    {"cmd":"tune","ch":1,"thr_step":1}      one settings step up / down
    {"cmd":"drive","ch":1,"drive":50000}    set laser duty (absolute)
    {"cmd":"drive","ch":1,"drive_step":-1}  one settings step
    {"cmd":"tune_get"}                      all active heads
    {"cmd":"tune_get","ch":1}               one head

Lines containing "align" are the sandheads scope (align_scope.py, client
SERIAL_CID): handed to Aligner.on_message, frames back through send().

Channel numbers are 1-based.  Adjustments are refused while the tower
is not quiet or a survey is running, so they never take the mux or
change a laser in the middle of stroke detection.  Raising the laser
duty is refused if the measured current is at or above laser.max_ma.
"""
import json
import time

from mux_errors import ChannelBusy
from signal_bands import COUNTS_PER_MA, FLOOR_COUNTS
from adc_avg import paced_avg
from threshold_refresh import clamp_duty, threshold_settings, _int
from ring import FULL

# Laser drive defaults (settings "laser").  25 % floor: README, below about
# half duty the sense current is still on the floor.  max_ma is the
# laser_measure "over" / socket_classify fault limit.
LASER_MIN_DUTY = 16384
LASER_MAX_DUTY = FULL          # same as PWM_DUTY_MAX / ring.FULL
LASER_STEP = 1311              # ~2 % of PWM_DUTY_MAX
LASER_MAX_MA = 60.0

ACTIVE_KINDS = ("head_new", "head_old")
NO_THRESHOLD_KINDS = ("head_old",)

TUNE_MIN_GAP_MS = 200          # at most one adjustment per head per this
# Laser mA: the laser current is chopped by its PWM (1 kHz), so a quick
# burst of reads catches a random part of on / off and the value jumps (at
# 25 % duty: 0.2-1 mA, now and then 22 mA).  So GP26 is read at TUNE_ADC_N
# points spread evenly over TUNE_ADC_PERIODS whole PWM periods (adc_avg
# .paced_avg): the mean is the average current; / duty: the on-current.
TUNE_ADC_N = 131               # GP26 reads per burst, prime (coprime with the
                               # period count).  time.monotonic_ns steps in
                               # 1/32768 s on the RP2350: 131 is one read per step
                               # over 4 ms, as fine as the pacing clock allows
TUNE_ADC_PERIODS = 4           # whole laser PWM periods per burst (4 ms at 1 kHz)
TUNE_BURST_MAX_MS = 5          # blocking cap (5-pin MIDI, the loop): fewer whole
                               # periods if the PWM is slow, never under one
TUNE_MA_BURSTS = 4             # mA is the mean of this many bursts at one duty
                               # (a drive change starts afresh): steadier, as each
                               # burst starts at a different PWM phase
LASER_HZ = 1000                # if the PWM cannot say: pins.laser_pwm_hz's default
MA_DP = 2                      # mA decimals (heads_cc.MA_UNITS is 0.01 mA)
NS_PER_S = 1000000000
NS_PER_MS = 1000000
NS_PER_US = 1000
TUNE_LINE_MAX_B = 256          # one inbound command line
TUNE_BUF_MAX_B = 512           # inbound reassembly cap; over: drop and resync
TUNE_POLL_S = 0.05             # how often poll() wants to be called
TUNE_ACK_TYPE = "tune_ack"
TUNE_GET_TYPE = "tune"
NEWLINE = b"\n"
ERR_CHARS = 80
# SerialTune counters on the serial heartbeat (body "tune"; strings there are
# cut to hb_slim.HB_UDP_STR_MAX): tune commands handled, sandheads align lines
# handed on, bad lines, poll errors and the last error; burst_us: the last
# laser-mA burst (Tuner.burst_us), to see its cost on the Pico
BRIEF_KEYS = ("lines", "align", "bad", "errors", "last_error", "burst_us")

KNOWN_CMDS = ("tune", "drive", "tune_get")
ALIGN_MARK = '"align"'          # a line naming it goes to align_scope (cmd "align")
# keys allowed on a command besides cmd (and ch, which is handled separately)
_TUNE_KEYS = frozenset(("cmd", "ch", "thr", "thr_step"))
_DRIVE_KEYS = frozenset(("cmd", "ch", "drive", "drive_step"))
_GET_KEYS = frozenset(("cmd", "ch"))


def _float(v, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:                  # NaN
        return default
    return x


def laser_settings(cfg):
    """Named laser-drive limits from settings (the state model).
    Missing or bad values become the defaults.  -> min_duty, max_duty,
    step, max_ma."""
    sec = cfg.get("laser") if cfg is not None and hasattr(cfg, "get") else None
    sec = sec if isinstance(sec, dict) else {}
    lo = _int(sec.get("min_duty"), LASER_MIN_DUTY)
    hi = _int(sec.get("max_duty"), LASER_MAX_DUTY)
    if lo > hi:
        lo, hi = hi, lo
    step = _int(sec.get("step"), LASER_STEP)
    if step < 1:
        step = LASER_STEP
    max_ma = _float(sec.get("max_ma"), LASER_MAX_MA)
    if max_ma <= 0:
        max_ma = LASER_MAX_MA
    return {"min_duty": lo, "max_duty": hi, "step": step, "max_ma": max_ma}


def tune_settings(cfg):
    """threshold_settings + laser_settings (+ min_gap_ms)."""
    t, l = threshold_settings(cfg), laser_settings(cfg)
    gap = _int((cfg.get("tune") or {}).get("min_gap_ms")
               if cfg is not None and hasattr(cfg, "get") and isinstance(cfg.get("tune"), dict)
               else TUNE_MIN_GAP_MS, TUNE_MIN_GAP_MS)
    if gap < 0:
        gap = TUNE_MIN_GAP_MS
    return {"threshold": t, "laser": l, "min_gap_ms": gap}


def parse_command(text, n_channels):
    """One inbound JSON line -> (cmd, ch, field, value).
    cmd is 'tune', 'drive' or 'tune_get'.  ch is 0-based, or None for
    tune_get of every head.  field is 'thr'/'drive' (absolute duty) or
    'thr_step'/'drive_step' (+1 / -1); None for tune_get.
    Raises ValueError(reason) if the line is not a valid command."""
    try:
        d = json.loads(text)
    except ValueError:
        raise ValueError("bad json")
    if not isinstance(d, dict):
        raise ValueError("not a command")
    cmd = d.get("cmd")
    if cmd not in KNOWN_CMDS:
        raise ValueError("unknown command")
    keys = set(d.keys())
    if cmd == "tune_get":
        if keys - _GET_KEYS:
            raise ValueError("unknown key")
        if "ch" not in d:
            return "tune_get", None, None, None
        ch = _ch(d.get("ch"), n_channels)
        return "tune_get", ch, None, None
    ch = _ch(d.get("ch"), n_channels)
    if cmd == "tune":
        if keys - _TUNE_KEYS:
            raise ValueError("unknown key")
        if "thr" in d and "thr_step" in d:
            raise ValueError("thr and thr_step")
        if "thr" in d:
            return "tune", ch, "thr", _abs_duty(d["thr"])
        if "thr_step" in d:
            return "tune", ch, "thr_step", _step_sign(d["thr_step"])
        raise ValueError("no thr")
    if keys - _DRIVE_KEYS:
        raise ValueError("unknown key")
    if "drive" in d and "drive_step" in d:
        raise ValueError("drive and drive_step")
    if "drive" in d:
        return "drive", ch, "drive", _abs_duty(d["drive"])
    if "drive_step" in d:
        return "drive", ch, "drive_step", _step_sign(d["drive_step"])
    raise ValueError("no drive")


def _ch(v, n_channels):
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= n_channels:
        raise ValueError("bad channel")
    return v - 1


def _abs_duty(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("bad duty")
    return int(v)


def _step_sign(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
        raise ValueError("bad step")
    return 1 if v > 0 else -1


class Tuner:
    """Live threshold and laser-duty store.  host (the Concentrator)
    provides: n, bells, mux, tower.tower, gate.kind, refresher
    (.target, .set_target, .readback, .default_duty, .min_duty, .max_duty),
    survey_state.phase, survey_state.last_result, and the survey-idle
    name (default 'idle').  Nothing is written to a file."""

    def __init__(self, host, cfg=None, clock_ms=None, survey_idle="idle", now_ns=None):
        self.host = host
        self.now_ns = now_ns or _now_ns
        self.settings = tune_settings(cfg)
        self.clock = clock_ms or _now_ms
        self._idle = survey_idle
        self.overrides = {}      # ch -> laser duty (RAM; passed to Ring.start)
        self.ma = {}             # ch -> last average laser mA (above dark)
        self.ma_on = {}          # ch -> that / duty: the current while on (None: duty 0)
        self.burst_us = None     # how long the last GP26 burst took
        self._bursts = {}        # ch -> [duty, [burst means, newest last]]
        self.pt = {}             # ch -> (volts, spread) of the last PT level (pt_level.py)
        self.last_adj = {}       # ch -> clock_ms of last successful adjust
        self.errors = 0
        self.last_error = None

    def drive_map(self):
        """{ch: duty} for Ring.start, so a re-survey keeps a live drive."""
        return dict(self.overrides)

    def handle(self, cmd, ch, field, value, now=None):
        """Apply one parsed command -> ack dict (never raises)."""
        try:
            return self._handle(cmd, ch, field, value, now)
        except Exception as e:
            self.errors += 1
            self.last_error = repr(e)[:ERR_CHARS]
            return self._ack("bad", ch, False, "error")

    def _handle(self, cmd, ch, field, value, now):
        if now is None:
            now = self.clock()
        if cmd == "tune_get":
            return self._get(ch)
        why = self._refuse(cmd, ch, now)
        if why is not None:
            return self._ack(cmd, ch, False, why)
        if field in ("thr", "thr_step"):
            return self._set_thr(ch, field, value, now)
        return self._set_drive(ch, field, value, now)

    def kind_of(self, ch):
        return self.host.gate.kind[ch]

    def _refuse(self, cmd, ch, now):
        h = self.host
        if ch is None or not 0 <= ch < h.n:
            return "bad channel"
        k = self.kind_of(ch)
        if k not in ACTIVE_KINDS:
            return "not an active head (%s)" % k
        if h.tower.tower != "inactive":
            return "tower not quiet (%s): strokes in progress" % h.tower.tower
        if h.survey_state.phase != self._idle:
            return "survey running"
        if cmd == "tune" and k in NO_THRESHOLD_KINDS:
            return "old head has no threshold"
        last = self.last_adj.get(ch)
        if last is not None and 0 <= now - last < self.settings["min_gap_ms"]:
            return "too fast"
        return None

    def _set_thr(self, ch, field, value, now):
        h = self.host
        s = self.settings["threshold"]
        cur = h.refresher.target.get(ch)
        if cur is None:
            cur = h.refresher.default_duty
        if field == "thr_step":
            new = clamp_duty(cur + value * s["step"], s["min_duty"], s["max_duty"])
            if new == cur and ch in h.refresher.target:
                return self._ack("tune", ch, False, "at limit")
        else:
            new = clamp_duty(value, s["min_duty"], s["max_duty"])
        h.refresher.set_target(h.bells[ch], new)
        self.last_adj[ch] = now
        return self._ack("tune", ch, True, None)

    def _set_drive(self, ch, field, value, now):
        h = self.host
        s = self.settings["laser"]
        bell = h.bells[ch]
        try:
            cur = int(bell.laser.duty_cycle)
        except Exception:
            cur = self.overrides.get(ch, 0)
        if cur <= 0:
            return self._ack("drive", ch, False, "laser is off")
        if field == "drive_step":
            new = clamp_duty(cur + value * s["step"], s["min_duty"], s["max_duty"])
            if new == cur:
                return self._ack("drive", ch, False, "at limit")
        else:
            new = clamp_duty(value, s["min_duty"], s["max_duty"])
        if new > cur:
            ma = self._measure(ch)
            if ma is False:
                return self._ack("drive", ch, False, "busy")
            cap = s["max_ma"]
            # the on-current, as laser_measure's "over" (DC, full duty)
            on = self.ma_on.get(ch)
            if on is not None:
                ma = on
            if ma is not None and ma >= cap:
                return self._ack("drive", ch, False,
                                 "laser current over %s mA" % _ma_str(cap))
        bell.laser.duty_cycle = new
        self.overrides[ch] = new
        self.last_adj[ch] = now
        return self._ack("drive", ch, True, None)

    def burst_ns(self, laser):
        """The GP26 burst: TUNE_ADC_PERIODS whole periods of the laser PWM,
        fewer if that is over TUNE_BURST_MAX_MS, never under one."""
        try:
            hz = int(laser.frequency)
        except Exception:
            hz = 0
        period = NS_PER_S // (hz if hz > 0 else LASER_HZ)
        k = min(TUNE_ADC_PERIODS, TUNE_BURST_MAX_MS * NS_PER_MS // period)
        return max(1, k) * period

    def _mean_of_bursts(self, ch, duty, counts):
        """Mean of the last TUNE_MA_BURSTS burst means at this duty."""
        rec = self._bursts.get(ch)
        if rec is None or rec[0] != duty:
            rec = self._bursts[ch] = [duty, []]
        got = rec[1]
        got.append(counts)
        if len(got) > TUNE_MA_BURSTS:
            got.pop(0)
        return sum(got) // len(got)

    def _measure(self, ch):
        """Average laser mA at the current duty (self.ma; above dark) and
        the on-current (self.ma_on = average / duty).  -> the average, or
        False if the mux is held (do not raise a drive without a reading)."""
        h = self.host
        bell = h.bells[ch]
        try:
            with h.mux.select(bell):
                counts, _, took = paced_avg(bell.sense.adc, TUNE_ADC_N,
                                            self.burst_ns(bell.laser), self.now_ns)
        except ChannelBusy:
            return False
        self.burst_us = took // NS_PER_US
        try:
            duty = int(bell.laser.duty_cycle)
        except Exception:
            duty = 0
        counts = self._mean_of_bursts(ch, duty, counts)
        dark = FLOOR_COUNTS
        r = None
        last = getattr(h, "survey_state", None)
        if last is not None:
            r = (last.last_result or {}).get(ch)
        if r:
            dark = (r.get("m") or {}).get("dark", dark)
        ma = round(max(0, counts - dark) / COUNTS_PER_MA, MA_DP)
        self.ma[ch] = ma
        self.ma_on[ch] = round(ma * FULL / duty, MA_DP) if duty > 0 else None
        self.read_pt(ch)
        return ma

    def read_pt(self, ch):
        """The raw PT level (GP27, no mux) when ch is the wired channel; no
        other channel has one.  -> (volts, spread) or None."""
        pt = getattr(self.host, "pt_level", None)
        got = pt.for_ch(ch) if pt is not None else None
        if got is not None:
            self.pt[ch] = got
        return got

    def _values(self, ch):
        h = self.host
        thr = h.refresher.target.get(ch)
        rb = h.refresher.readback.get(ch)
        try:
            drv = int(h.bells[ch].laser.duty_cycle)
        except Exception:
            drv = self.overrides.get(ch)
        pt = self.pt.get(ch)
        return {"ch": ch + 1, "thr": thr, "rb": rb, "drv": drv,
                "ma": self.ma.get(ch), "ma_on": self.ma_on.get(ch),
                # volts on the Orange/White wire; null: not fitted on this channel
                "pt": pt[0] if pt else None, "pt_span": pt[1] if pt else None}

    def _ack(self, what, ch, ok, why, typ=TUNE_ACK_TYPE):
        body = {"t": typ, "cmd": what, "ok": bool(ok), "why": why}
        if ch is not None and 0 <= ch < self.host.n:
            body.update(self._values(ch))
        else:
            body["ch"] = None if ch is None else ch
        return body

    def _get(self, ch):
        h = self.host
        if ch is not None:
            if not 0 <= ch < h.n:
                return self._ack("tune_get", ch, False, "bad channel")
            if h.tower.tower == "inactive" and h.survey_state.phase == self._idle:
                if self.kind_of(ch) in ACTIVE_KINDS:
                    got = self._measure(ch)
                    busy = got is False
                else:
                    busy = False
            else:
                busy = True
            body = self._ack("tune_get", ch, True, None, typ=TUNE_GET_TYPE)
            body["busy"] = busy
            return body
        heads = []
        for i in range(h.n):
            if self.kind_of(i) in ACTIVE_KINDS:
                heads.append(self._values(i))
        return {"t": TUNE_GET_TYPE, "cmd": "tune_get", "ok": True, "why": None,
                "heads": heads}


def _ma_str(v):
    if int(v) == v:
        return str(int(v))
    return "%.1f" % v


def _now_ms():
    return int(time.monotonic() * 1000)


def _now_ns():
    return time.monotonic_ns()


class SerialTune:
    """Read JSON command lines from usb_cdc.data and write acks, never
    blocking, never raising.  port: the same object SerialStatus writes.
    tuner.handle(...) produces the ack dict."""

    def __init__(self, port, tuner, dumps=None, max_line=TUNE_LINE_MAX_B,
                 buf_max=TUNE_BUF_MAX_B):
        self.port = port
        self.tuner = tuner
        self.dumps = dumps or _dumps
        self.max_line = max_line
        self.buf_max = buf_max
        try:
            port.timeout = 0
        except Exception:
            pass
        self._buf = bytearray()
        self._ack = None         # memoryview of an ack being written
        self._off = 0
        self._next = None        # the newest line waiting behind a part-written one
        self.lines = 0           # commands handled
        self.acks = 0            # ack lines fully written
        self.overflows = 0
        self.bad = 0
        self.dropped_ack = 0
        self.errors = 0
        self.last_error = None
        # align_scope over this port: align_rx(text) takes a {"cmd":"align"}
        # line (Aligner.on_message under SERIAL_CID: queue only); other_busy()
        # True while SerialStatus is mid-line (do not start a frame then)
        self.align_rx = None
        self.other_busy = None
        self.aligned = 0         # align lines handed on

    def counters(self):
        return {"lines": self.lines, "acks": self.acks,
                "overflows": self.overflows, "bad": self.bad,
                "dropped_ack": self.dropped_ack, "errors": self.errors,
                "last_error": self.last_error, "align": self.aligned,
                "burst_us": self.tuner.burst_us}

    def brief(self):
        """BRIEF_KEYS of counters(), for the serial heartbeat (body "tune"):
        a failing reader shows on the page instead of only here."""
        c = self.counters()
        return dict((k, c[k]) for k in BRIEF_KEYS)

    def busy(self):
        """True while an ack / frame is part-written (SerialStatus waits)."""
        return self._ack is not None

    def send(self, obj):
        """An align frame / ack for the USB host (Aligner's send_to).  None:
        no host has the port open (the scope stops); True: written or held
        (one line in flight; a newer one replaces a held older one)."""
        if not self._connected():
            return None
        ob = self.other_busy
        self._queue_ack(obj, bool(ob()) if ob is not None else False)
        return True

    def _connected(self):
        return bool(getattr(self.port, "connected", True))

    def _read(self):
        n = getattr(self.port, "in_waiting", None)
        if n is None:
            # some fakes / ports have only read(); try a chunk
            n = 64
        if not n:
            return b""
        room = self.buf_max - len(self._buf)
        if room <= 0:
            self._buf = bytearray()
            self.overflows += 1
            room = self.buf_max
        data = self.port.read(n if n < room else room) or b""
        return bytes(data)

    def _push_ack(self):
        if self._ack is None:
            return
        n = self.port.write(self._ack[self._off:]) or 0
        self._off += n
        if self._off >= len(self._ack):
            self.acks += 1
            nxt, self._next = self._next, None
            self._ack = None if nxt is None else memoryview(nxt)
            self._off = 0

    def _queue_ack(self, obj, busy):
        """One line out: written now if it can be, else held.  A held line
        not yet started is replaced by a newer one (counted dropped); a
        part-written one is always finished first (a cut line would spoil
        both on the host), the newest waiting behind it."""
        data = (self.dumps(obj) + "\n").encode("utf-8")
        if self._ack is not None:
            if self._off > 0:
                if self._next is not None:
                    self.dropped_ack += 1
                self._next = data
                return
            self.dropped_ack += 1
        self._ack = memoryview(data)
        self._off = 0
        if not busy:
            self._push_ack()

    def _one_line(self, raw, now, busy):
        if len(raw) > self.max_line:
            self.bad += 1
            self._queue_ack(self.tuner._ack("bad", None, False, "too long"), busy)
            return
        try:
            text = raw.decode("utf-8")
            if self.align_rx is not None and ALIGN_MARK in text:
                # the sandheads scope: Aligner parses and acks it (bad ones too)
                self.aligned += 1
                self.align_rx(text)
                return
            cmd, ch, field, value = parse_command(text, self.tuner.host.n)
        except (ValueError, UnicodeError) as e:
            self.bad += 1
            why = str(e) if isinstance(e, ValueError) else "bad json"
            self._queue_ack(self.tuner._ack("bad", None, False, why), busy)
            return
        self.lines += 1
        ack = self.tuner.handle(cmd, ch, field, value, now)
        self._queue_ack(ack, busy)

    def poll(self, now, busy=False):
        """Read available bytes, handle complete lines, push a part-written
        ack.  busy True: do not start a write (SerialStatus is mid-line).
        -> seconds until the next poll.  Never raises."""
        now_ms = int(now * 1000)
        try:
            if self._ack is not None and not busy and self._connected():
                self._push_ack()
            # read even while the host's DTR is down: a line it wrote is
            # still a line (the replies wait for connected)
            chunk = self._read()
            if chunk:
                self._buf += chunk
                # a fill to the cap with no newline: drop and resync
                if len(self._buf) >= self.buf_max and b"\n" not in self._buf:
                    self._buf = bytearray()
                    self.overflows += 1
                while True:
                    i = self._buf.find(b"\n")
                    if i < 0:
                        break
                    raw = bytes(self._buf[:i]).rstrip(b"\r")
                    # a new bytearray, not `del self._buf[:i + 1]`: CircuitPython
                    # 10.0.3 bytearrays have no slice deletion (TypeError, so
                    # every command line was dropped; tests/test_cp_buffers.py)
                    self._buf = self._buf[i + 1:]
                    if raw:
                        self._one_line(raw, now_ms, busy)
        except Exception as e:
            self.errors += 1
            self.last_error = repr(e)[:ERR_CHARS]
            self._buf = bytearray()
            self._ack = self._next = None
            self._off = 0
        return TUNE_POLL_S


def _dumps(obj):
    try:
        from hb_slim import dumps_compact     # no Wi-Fi module (ring_send "off")
        return dumps_compact(obj)
    except ImportError:
        return json.dumps(obj, separators=(",", ":"))


def open_serial_tune(port, tuner, log=print):
    """SerialTune on the USB data port, or None (no port)."""
    if port is None or tuner is None:
        return None
    log("tune: commands on the USB data port (RAM only; settings.json is not written)")
    return SerialTune(port, tuner)
