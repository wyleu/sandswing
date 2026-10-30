# sense_keys.py -- the optical sense inputs as stamped edges (CircuitPython keypad).
#
# The concentrator used to read every sense pin from its main loop and stamp an
# edge with the time it got round to looking.  A busy loop (a WebSocket send, a
# radio pass) made that late: a 3200 ms test pulse train on ch 4 read 3156-3247 ms
# gaps, in long/short pairs.  keypad.Keys scans the pins in the background every
# inputs.scan_interval_ms and queues each change with its own
# supervisor.ticks_ms timestamp, so the time of an edge no longer depends on when
# the loop drains the queue.
#
# One owner, two modes, never both at once (CircuitPython lets one object hold a
# pin):
#   keys  keypad.Keys scans; edges come from drain() as (ch, held, raw_tick).
#   pins  plain DigitalInOut inputs (pull-up) for the socket survey and the boot
#         self-test, which switch pulls and sample fast.  to_pins() / to_keys()
#         hand the pins over; edges queued before the hand-over are kept.
#
# Polarity is the one the old pin-reading code used: active-low sense with the
# internal pull-up (DigitalInOut pull=UP, held = not pin.value).  So
# value_when_pressed=False, pull=True, and "pressed" means held.
#
# raw_tick is supervisor.ticks_ms (wraps at 2**29).  Callers map it with
# sntp_pico.TickClock.extend() / SntpClock.stamp(raw_tick), never with
# arithmetic of their own.
#
# Host tests use tests/fakes/keypad.py.

# settings.json "inputs" (config_loader.DEFAULTS has the same values)
SCAN_INTERVAL_MS = 1      # scan every 1 ms tick: edge stamps are good to 1 ms
# 64 = keypad's own default.  Each stroke is two events (break + clear) per
# channel, plus any bounce.  Eight channels ringing hard (~1 stroke/s each)
# make ~16 events/s, so 64 holds ~4 s of edges between drains, longer than the
# worst loop hold logged so far (a Wi-Fi rejoin pauses the loop, and edges are
# also pumped before and after it).  The queue costs a few bytes per event.
EVENT_QUEUE_LEN = 64

VALUE_WHEN_PRESSED = False   # active-low: the PT pulls the line low when held
PULL = True                  # internal pull-up, as DigitalInOut Pull.UP before
MS_PER_S = 1000
KEYS, PINS = "keys", "pins"


def settings(cfg):
    """(scan_interval_ms, event_queue_len) from cfg "inputs", else the
    defaults above.  cfg: a config_loader.Config or a plain dict."""
    sec = cfg.get("inputs", {}) if cfg is not None else {}
    sec = sec or {}
    scan = sec.get("scan_interval_ms", SCAN_INTERVAL_MS)
    qlen = sec.get("event_queue_len", EVENT_QUEUE_LEN)
    try:
        scan = max(1, int(scan))
    except (TypeError, ValueError):
        scan = SCAN_INTERVAL_MS
    try:
        qlen = max(1, int(qlen))
    except (TypeError, ValueError):
        qlen = EVENT_QUEUE_LEN
    return scan, qlen


class SensePin:
    """Stable stand-in for one sense pin (bell.sense_pin, Ring's inputs).
    pins mode: the live DigitalInOut (value and pull).  keys mode: value is
    the level keypad last reported (pull cannot be changed)."""

    def __init__(self, owner, ch):
        self._o = owner
        self.ch = ch

    @property
    def value(self):
        return self._o.value(self.ch)

    @property
    def pull(self):
        return self._o.dio(self.ch).pull

    @pull.setter
    def pull(self, p):
        self._o.dio(self.ch).pull = p


class SenseKeys:
    def __init__(self, gps, scan_interval_ms=SCAN_INTERVAL_MS,
                 queue_len=EVENT_QUEUE_LEN, log=print):
        self.gps = [int(g) for g in gps]
        self.n = len(self.gps)
        self.scan_interval_ms = int(scan_interval_ms)
        self.queue_len = int(queue_len)
        self.log = log
        self.held = [False] * self.n     # latest level known (True = held)
        self.overflows = 0               # keypad queue overflowed (edges lost)
        self.handovers = 0               # keys -> pins -> keys round trips
        self.mode = None
        self.keys = None
        self._dio = None
        self._pending = []               # (ch, held, raw_tick) not yet drained
        self._ev = None                  # one reused keypad.Event (no allocation)
        self.pins = [SensePin(self, ch) for ch in range(self.n)]
        self.to_pins()

    # ---- hand-over --------------------------------------------------------
    def _board_pin(self, gp):
        import board
        return getattr(board, "GP%d" % gp)

    def to_pins(self):
        """Plain inputs for the survey.  Queued edges are kept for drain()."""
        if self.mode == PINS:
            return
        import digitalio
        if self.keys is not None:
            self.pump()
            self.keys.deinit()
            self.keys = None
        pins = []
        for gp in self.gps:
            p = digitalio.DigitalInOut(self._board_pin(gp))
            p.direction = digitalio.Direction.INPUT
            p.pull = digitalio.Pull.UP
            pins.append(p)
        self._dio = pins
        self._read_levels(first=self.mode is None)
        self.mode = PINS

    def to_keys(self):
        """Hand the pins to keypad.  The level read here is the starting
        state; keypad's first scan reports keys already held as pressed,
        which pump() drops because the level is already known."""
        if self.mode == KEYS:
            return
        import keypad
        if self._dio is not None:
            self._read_levels()
            for p in self._dio:
                p.deinit()
            self._dio = None
        self.keys = keypad.Keys([self._board_pin(gp) for gp in self.gps],
                                value_when_pressed=VALUE_WHEN_PRESSED, pull=PULL,
                                interval=self.scan_interval_ms / MS_PER_S,
                                max_events=self.queue_len)
        self._ev = keypad.Event()
        if self.mode == PINS:
            self.handovers += 1
        self.mode = KEYS

    def _read_levels(self, first=False):
        """Read the pins.  A level that differs from the last one known is
        queued as an edge with no stamp (raw_tick None: it changed while
        nothing was watching).  The first read at boot just sets the levels."""
        for ch, p in enumerate(self._dio):
            held = p.value == VALUE_WHEN_PRESSED
            if held != self.held[ch] and not first:
                self._pending.append((ch, held, None))
            self.held[ch] = held

    def dio(self, ch):
        if self.mode != PINS:
            raise RuntimeError("sense ch %d: keypad owns the pin" % (ch + 1))
        return self._dio[ch]

    def value(self, ch):
        """The pin level as DigitalInOut.value would read it."""
        if self.mode == PINS:
            return self._dio[ch].value
        self.pump()
        return VALUE_WHEN_PRESSED if self.held[ch] else not VALUE_WHEN_PRESSED

    # ---- edges ------------------------------------------------------------
    def pump(self):
        """Move keypad's queued edges into our own list (call before and
        after anything that blocks).  Returns how many were moved."""
        k = self.keys
        if k is None:
            return 0
        q, ev, moved = k.events, self._ev, 0
        while q.get_into(ev):
            ch = ev.key_number
            if not 0 <= ch < self.n:
                continue
            held = ev.pressed
            if held == self.held[ch]:
                continue                 # no change (first scan of a held key)
            self.held[ch] = held
            if len(self._pending) >= self.queue_len:
                self._lost("our pending list is full (%d)" % self.queue_len)
                continue
            self._pending.append((ch, held, ev.timestamp))
            moved += 1
        if q.overflowed:
            self._lost("keypad queue overflowed (max_events %d)" % self.queue_len)
            q.clear()                    # the only way to reset overflowed
        return moved

    def _lost(self, why):
        self.overflows += 1
        self.log("sense: EDGES LOST, %s; overflow %d this boot" % (why, self.overflows))

    def drain(self):
        """Edges in the order they happened: [(ch, held, raw_tick), ...].
        raw_tick is the keypad stamp (supervisor.ticks_ms), or None for a
        change seen at a pins/keys hand-over."""
        self.pump()
        if not self._pending:
            return ()
        out, self._pending = self._pending, []
        return out

    def status(self):
        return {"mode": self.mode, "scan_ms": self.scan_interval_ms,
                "queue": self.queue_len, "overflows": self.overflows,
                "handovers": self.handovers}

    def deinit(self):
        if self.keys is not None:
            self.keys.deinit()
            self.keys = None
        if self._dio is not None:
            for p in self._dio:
                p.deinit()
            self._dio = None
        self.mode = None
