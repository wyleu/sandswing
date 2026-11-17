"""
align_scope.py -- the sandheads page's scope for ONE head at a time.
Protocol and safety rules: docs/align-scope.md.

The page (Sandbells /farm/heads/, changes/bells/static/js/align.js) holds a
/ws socket to the Pico.  While it keeps asking (every ~2 s; the scope stops
itself after KEEPALIVE_MS without a request) the Pico:

  * records the comparator level of that head once per sense pass into a
    preallocated ring (a beam break shorter than a pass still shows: an edge
    to "held" since the last sample makes that sample low);
  * about every FRAME_MS queues ONE frame for that client only: the last
    ~2 s as hex bits, laser mA, GP28 readback, threshold and drive duty,
    and the raw PT level (GP27) on the wired channel;
  * carries out {"thr": +-1} / {"drive": +-1} through tune.Tuner (the same
    rules, limits and RAM-only store as the USB tune commands).

The same protocol also runs over the USB data port (ring_send "off", no
Wi-Fi): Sandbells' ingest_serial relays the page's commands as lines and the
readings and acks come back as MIDI CC on the USB MIDI out (heads_cc.py;
JSON lines on the data port only if there is no USB MIDI), at
SERIAL_FRAME_MS (client SERIAL_CID).

Live only: nothing here writes settings.json.

Reliability: no allocation per sample; on_message only queues (it runs in
the /ws poll); everything else runs in Concentrator.align_task; every entry
point catches everything; MAX_ERRORS in a boot switch the scope off.  The
mux is only taken (Tuner._measure) while the tower is quiet and no survey
runs, and only every MEASURE_MS.
"""
import json
import binascii
import heads_cc
import head_store
from array import array

SCHEMA = "sand.panel/v1"        # ingest ignores it; frames go to one client anyway
T_FRAME = "align"
T_ACK = "align_ack"
T_INFO = "align_info"

RING_N = 128                    # samples kept (power of two): > 2 s at ~20 ms a pass
WINDOW_MS = 2000                # how much of the ring one frame carries
MASK29 = (1 << 29) - 1          # supervisor.ticks_ms wraps here
FRAME_MS = 200                  # one frame about this often
FRAME_BUSY_MS = 400             # ... half as often while the tower is not quiet
MEASURE_MS = 400                # laser mA (mux + a ~4 ms GP26 burst, tune.py) at most this often
KEEPALIVE_MS = 10000            # no request for this long: the scope stops
IDLE_S = 0.25                   # align_task period with no scope running
MAX_PENDING = 4                 # queued commands; more are dropped (counted)
MSG_MAX_B = 256                 # one inbound command
MAX_ERRORS = 10                 # errors in a boot: the scope is off until reboot
# Free VM heap below which a scope will not run.  The heap grows on demand
# from CIRCUITPY_HEAP_START_SIZE, so the boot's ~17 kB free is not a limit;
# this only stops the scope adding JSON garbage when memory really is short.
MEM_MIN = 8192
# The USB data port (ring_send "off": no /ws).  tune.SerialTune hands the
# host's {"cmd":"align"} lines in under this client id and writes our frames
# back on usb_cdc.data.  Slower than /ws: the port also carries the heartbeat
# and LED lines, and every frame is a few hundred bytes of JSON garbage on a
# heap with ~14 kB free.
SERIAL_CID = "usb"
SERIAL_FRAME_MS = 500           # one frame this often to the USB host
SERIAL_FRAME_BUSY_MS = 1000     # ... while the tower is not quiet

ACTIVE_KINDS = ("head_new", "head_old")     # = tune.ACTIVE_KINDS, the page's ACTIVE
_KEYS = ("cmd", "ch", "scope", "thr", "drive", "info", "thr_mid",
         "sweep", "use", "save", "label", "forget", "saved")
SWEEP_TICK_S = 0.05             # = sweep.SWEEP_TICK_S (sweep.py loads on the first sweep)
T_SWEEP = "align_sweep"         # a sweep point (the /ws path; USB: heads_cc point)
T_SLOT = "align_slot"           # one channel's saved line-up values (head_store)
# page key -> (tune.Tuner command, field)
_TUNE = {"thr": ("tune", "thr_step"), "drive": ("drive", "drive_step")}


class SampleRing:
    """Preallocated ring of 0/1 samples with their tick (ms) stamps."""

    def __init__(self, n=RING_N):
        if n & (n - 1):
            raise ValueError("ring size must be a power of two")
        self.n = n
        self.bits = bytearray(n)
        self.ts = array("I", [0] * n)
        self.pack = bytearray((n + 7) // 8)
        self.head = 0
        self.count = 0

    def clear(self):
        self.head = 0
        self.count = 0

    def add(self, bit, now_ms):
        i = self.head
        self.bits[i] = 1 if bit else 0
        self.ts[i] = now_ms & MASK29
        self.head = (i + 1) & (self.n - 1)
        if self.count < self.n:
            self.count += 1

    def window(self, window_ms=WINDOW_MS):
        """Newest samples within window_ms -> (n, mean interval ms, longest gap ms)."""
        c = self.count
        if c == 0:
            return 0, 0, 0
        mask = self.n - 1
        newest = (self.head - 1) & mask
        t_new = self.ts[newest]
        k, gap, prev = 1, 0, t_new
        while k < c:
            t = self.ts[(newest - k) & mask]
            if ((t_new - t) & MASK29) > window_ms:
                break
            d = (prev - t) & MASK29
            if d > gap:
                gap = d
            prev = t
            k += 1
        if k < 2:
            return k, 0, 0
        return k, ((t_new - prev) & MASK29) / (k - 1), gap

    def hex_bits(self, k):
        """Newest k samples, oldest first, 1 = high, MSB first, as hex."""
        mask = self.n - 1
        oldest = (self.head - k) & mask
        pack = self.pack
        nbytes = (k + 7) >> 3
        for j in range(nbytes):
            pack[j] = 0
        bits = self.bits
        for j in range(k):
            if bits[(oldest + j) & mask]:
                pack[j >> 3] |= 0x80 >> (j & 7)
        return binascii.hexlify(memoryview(pack)[:nbytes]).decode()


def unpack_bits(hexstr, k):
    """Inverse of hex_bits (tests; align.js decodeRuns does the same)."""
    raw = binascii.unhexlify(hexstr)
    return [1 if raw[j >> 3] & (0x80 >> (j & 7)) else 0 for j in range(k)]


def parse_command(text, n_channels):
    """One {"cmd":"align", ...} frame -> (kind, ch0, value):
    ("info", None, True), ("scope", ch0, bool), ("thr"|"drive", ch0, +1|-1).
    Raises ValueError(reason)."""
    try:
        d = json.loads(text)
    except ValueError:
        raise ValueError("bad json")
    if not isinstance(d, dict) or d.get("cmd") != "align":
        raise ValueError("not an align command")
    for k in d:
        if k not in _KEYS:
            raise ValueError("unknown key")
    if d.get("info"):
        return "info", None, True
    if "label" in d and "save" not in d:
        raise ValueError("label without save")
    if d.get("saved") is True:
        return "saved", None, True
    if "forget" in d:
        return "forget", None, _slot(d["forget"])
    ch = d.get("ch")
    if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= n_channels:
        raise ValueError("bad channel")
    if "scope" in d:
        return "scope", ch - 1, bool(d["scope"])
    if d.get("thr_mid") is True:
        return "thr_mid", ch - 1, True
    if "sweep" in d:
        if not isinstance(d["sweep"], bool):
            raise ValueError("bad sweep")
        return "sweep", ch - 1, d["sweep"]
    if "use" in d:
        u = d["use"]
        if (not isinstance(u, list) or len(u) != 2 or not _duty_ok(u[0])
                or not (u[1] is None or _duty_ok(u[1]))):
            raise ValueError("bad use")
        return "use", ch - 1, (u[0], u[1])
    if "save" in d:
        return "save", ch - 1, (_slot(d["save"]), head_store.check_label(d.get("label")))
    for key in ("thr", "drive"):
        if key in d:
            v = d[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
                raise ValueError("bad step")
            return key, ch - 1, 1 if v > 0 else -1
    raise ValueError("nothing to do")


def _duty_ok(v):
    return not isinstance(v, bool) and isinstance(v, int) and 0 <= v <= heads_cc.DUTY_FULL


def _slot(v):
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= head_store.SLOTS:
        raise ValueError("bad channel")
    return v - 1


class Aligner:
    """host: the Concentrator (n, gate.kind, tower.tower, survey_state.phase,
    refresher.target / .readback, bells[ch].laser, tune, _prev_held,
    pt_level).  send_to(client_id, obj) -> True / False / None (gone)."""

    def __init__(self, host, send_to, clock, ident, mem_free=None, log=print,
                 survey_idle="idle", cc=None, cc_alive=None):
        self.host = host
        # the USB host (SERIAL_CID): readings and acks as MIDI CC (heads_cc.CcOut)
        # instead of JSON lines; cc_alive() False: the host closed the data port
        self.cc = cc
        self.cc_alive = cc_alive
        self._broke = False         # a low sample since the last CC report
        self.send_to = send_to
        self.clock = clock
        self.mem_free = mem_free
        self.log = log
        self._idle = survey_idle
        self.ring = SampleRing()
        self.pending = []
        self.ch = None              # 0-based head being scoped
        self.cid = None             # the client that asked last
        self.last_req = 0
        self.next_frame = 0
        self.last_measure = None
        self._low = False           # an edge to held since the last sample
        self.errors = 0
        self.dropped = 0
        self.frames = 0
        self.dead = False
        self.sweep = None           # sweep.Sweep while one runs
        self.sweep_cid = None
        self.sweep_ch = None
        self.store = getattr(host, "head_store", None)   # head_store.HeadStore (NVM)
        self.slots_out = []         # saved-values reports still to send, one a tick
        self.slots_cid = None
        src, dev, boot = ident
        self._body = {}
        self._doc = {"schema": SCHEMA, "type": T_FRAME, "src": src, "id": dev,
                     "boot_id": boot, "tick_ms": 0, "body": self._body}

    # ---- called from the /ws poll: queue only ------------------------------
    def on_message(self, cid, text):
        try:
            if self.dead:
                return
            if len(text) > MSG_MAX_B:
                raise ValueError("too long")
            if len(self.pending) >= MAX_PENDING:
                self.dropped += 1
                return
            self.pending.append((cid, text))
        except Exception:
            self._error()

    # ---- called from the sense pass: no allocation -------------------------
    def edge(self, i, held):
        if held:
            if i == self.ch:
                self._low = True
        sw = self.sweep
        if sw is not None and i == sw.ch and not self.host.tune.lit(held):
            sw.dark = True              # light lost on the swept head (heads.pt_light)

    def sample(self):
        ch = self.ch
        if ch is None:
            return
        try:
            bit = 0 if (self._low or self.host._prev_held[ch]) else 1
            self._low = False
            if not bit:
                self._broke = True
            self.ring.add(bit, self.clock())
        except Exception:
            self._error()

    # ---- align_task ---------------------------------------------------------
    def _frame_ms(self):
        quiet = self._quiet()
        if self.cid == SERIAL_CID:
            return SERIAL_FRAME_MS if quiet else SERIAL_FRAME_BUSY_MS
        return FRAME_MS if quiet else FRAME_BUSY_MS

    def period_s(self):
        if not self.dead and (self.sweep is not None or self.slots_out):
            return SWEEP_TICK_S
        if self.ch is None or self.dead:
            return IDLE_S
        return self._frame_ms() / 1000

    def tick(self):
        try:
            if self.dead:
                return
            now = self.clock()
            while self.pending:
                cid, text = self.pending.pop(0)
                self._command(cid, text, now)
            if self.sweep is not None:
                self._sweep_step(now)
            if self.slots_out:
                self._slot_out(self.slots_out.pop(0))
            if self.ch is None:
                return
            why = self._stop_why(now)
            if why is not None:
                self._stop(why)
                return
            if now - self.next_frame >= 0:
                self.next_frame = now + self._frame_ms()
                self._frame(now)
        except Exception:
            self._error()

    # ---- inside ---------------------------------------------------------------
    def _error(self):
        self.errors += 1
        sw = self.sweep
        if sw is not None:
            self.sweep = None
            try:
                sw.stop("error")    # the drive goes back whatever went wrong
            except Exception:
                pass
        if self.errors >= MAX_ERRORS and not self.dead:
            self.dead = True
            self.ch = None
            try:
                self.log("align scope off for this boot: %d errors" % self.errors)
            except Exception:
                pass

    def _quiet(self):
        h = self.host
        return h.tower.tower == "inactive" and h.survey_state.phase == self._idle

    def _stuck(self):
        """Not quiet only because of stuck heads (tune.Tuner.unlocked): thr /
        drive go through, so the scope measures and sends full reports."""
        h = self.host
        try:
            return h.survey_state.phase == self._idle and h.tune.unlocked()
        except Exception:
            return False

    def _lit_low(self):
        try:
            return bool(self.host.tune.light_lowers)
        except Exception:
            return True

    def _adjustable(self):
        return self._quiet() or self._stuck()

    def kind_of(self, ch):
        return self.host.gate.kind[ch]

    def active(self):
        return [i + 1 for i in range(self.host.n) if self.kind_of(i) in ACTIVE_KINDS]

    def _stop_why(self, now):
        if now - self.last_req > KEEPALIVE_MS:
            return "timeout"
        if self.kind_of(self.ch) not in ACTIVE_KINDS:
            return "head gone"
        if self.mem_free is not None and self.mem_free() < MEM_MIN:
            return "low memory"
        return None

    def _stop(self, why):
        ch, cid = self.ch, self.cid
        self.ch = None
        self.ring.clear()
        if why is not None and cid is not None:
            self._ack(cid, "scope", ch, False, why)

    def _command(self, cid, text, now):
        try:
            kind, ch, val = parse_command(text, self.host.n)
        except ValueError as e:
            self._ack(cid, "bad", None, False, str(e))
            return
        if kind == "info":
            self._info(cid)
        elif kind in ("sweep", "use", "save", "forget", "saved"):
            self._lineup(cid, kind, ch, val, now)
        elif kind == "scope":
            self._scope(cid, ch, val, now)
        else:
            self._nudge(cid, kind, ch, val, now)

    def _scope(self, cid, ch, on, now):
        if not on:
            if self.ch == ch:
                self.ch = None
                self.ring.clear()
            self._ack(cid, "scope", ch, True, None)
            return
        k = self.kind_of(ch)
        if k not in ACTIVE_KINDS:
            self._ack(cid, "scope", ch, False, "not an active head (%s)" % k)
            return
        if self.mem_free is not None and self.mem_free() < MEM_MIN:
            self._ack(cid, "scope", ch, False, "low memory")
            return
        self.cid = cid              # the newest asker gets the frames
        self.last_req = now
        if self.ch == ch:
            return                  # keep-alive: no ack
        self.ch = ch
        self.ring.clear()
        self._low = False
        self.next_frame = now
        self.last_measure = None
        self._ack(cid, "scope", ch, True, None)

    def _nudge(self, cid, key, ch, sign, now):
        t = self.host.tune
        if key == "thr_mid":
            # mid-range of the threshold limits (32768 with the defaults)
            s = t.settings["threshold"]
            a = t.handle("tune", ch, "thr", (s["min_duty"] + s["max_duty"] + 1) // 2, now)
        else:
            cmd, field = _TUNE[key]
            a = t.handle(cmd, ch, field, sign, now)
        self._ack(cid, key, ch, a.get("ok"), a.get("why"))

    # ---- line-up: sweep, use, save / forget in NVM ---------------------------
    def _busy_why(self):
        if not self._quiet():
            return "tower not quiet"
        if self.sweep is not None:
            return "sweep running"
        return None

    def _lineup(self, cid, kind, ch, val, now):
        h = self.host
        if kind == "sweep":
            self._sweep_cmd(cid, ch, val, now)
            return
        if kind == "use":
            a = h.tune.use_pair(ch, val[0], val[1], now)
            self._ack(cid, "use", ch, a.get("ok"), a.get("why"))
            return
        if kind == "saved":
            self.slots_cid = cid
            self.slots_out = list(range(head_store.SLOTS))
            self._ack(cid, "saved", None, True, None)
            return
        why = self._busy_why()
        if why is None and self.store is None:
            why = "not saved: no NVM"
        if why is None and kind == "save":
            if self.kind_of(ch) not in ACTIVE_KINDS:
                why = "not an active head"
            else:
                slot, label = val
                thr, drv = self._values(ch)
                why = self.store.save(slot, drv, thr, label)
        elif why is None:
            slot = val
            why = self.store.forget(slot)
        if why is None:
            self.slots_cid = cid
            self.slots_out.append(slot)
        self._ack(cid, kind, ch, why is None, why)

    def _sweep_cmd(self, cid, ch, on, now):
        h = self.host
        sw = self.sweep
        if not on:
            if sw is not None and sw.ch == ch:
                self.sweep = None
                sw.stop("stopped")
            self._ack(cid, "sweep", ch, True, None)
            return
        pt = getattr(h, "pt_level", None)
        why = self._busy_why()
        if why is None:
            if self.kind_of(ch) not in ACTIVE_KINDS:
                why = "not an active head"
            elif pt is None or pt.ch != ch:
                why = "no PT wire on this channel"
            elif h.survey_state.phase != self._idle:
                why = "survey running"
            elif not h.tune.lit(h._prev_held[ch]):
                why = "no light on the PT"
            elif self.mem_free is not None and self.mem_free() < MEM_MIN:
                why = "low memory"
        if why is None:
            try:
                d = int(h.bells[ch].laser.duty_cycle)
            except Exception:
                d = 0
            if d <= 0:
                why = "laser is off"
        if why is not None:
            self._ack(cid, "sweep", ch, False, why)
            return
        self.sweep_cid = cid
        self.sweep_ch = ch
        try:
            import sweep as sweep_mod                # loaded only when a sweep is asked
            sw = sweep_mod.Sweep(h, ch, now, self._sweep_report,
                                 sweep_mod.sweep_settings(h.tune.cfg))
        except Exception as e:
            h.bells[ch].laser.duty_cycle = d         # as it was
            h.tune.sweep_ch = None
            self._ack(cid, "sweep", ch, False, "low memory" if isinstance(e, MemoryError)
                      else "error")
            return
        self.sweep = sw
        self._ack(cid, "sweep", ch, True, None)

    def _sweep_step(self, now):
        sw = self.sweep
        if self.sweep_cid == SERIAL_CID and self.cc_alive is not None and not self.cc_alive():
            self.sweep = None
            sw.stop("stopped")                       # the host closed the data port
            return
        if not sw.step(now):
            self.sweep = None

    def _sweep_report(self, kind, *a):
        ch = self.sweep_ch
        cid = self.sweep_cid
        if kind == "end":
            self._ack(cid, "sweep_end", ch, a[0] is None, a[0])
            return
        i, drv, ma, pt, span = a
        if cid == SERIAL_CID and self.cc is not None:
            self.cc.point(ch + 1, self._cc_flags(ch), i, drv, ma, pt, span)
            return
        self.send_to(cid, self._env(T_SWEEP, {"ch": ch + 1, "i": i, "drv": drv, "ma": ma,
                                              "pt": pt, "pt_span": span}))

    def _slot_out(self, slot):
        st = self.store
        rec = st.slots[slot] if st is not None else None
        cid = self.slots_cid
        if cid == SERIAL_CID and self.cc is not None:
            self.cc.slot(slot + 1, rec)
            return
        self.send_to(cid, self._env(T_SLOT, {
            "ch": slot + 1, "saved": rec is not None,
            "drv": rec[0] if rec else None, "thr": rec[1] if rec else None,
            "label": rec[2] if rec else None,
            "state": st.state if st is not None else "none"}))

    def _cc_flags(self, ch):
        h = self.host
        f = 0
        if ch is not None and 0 <= ch < h.n and not h._prev_held[ch]:
            f |= heads_cc.FLAG_CLEAR
        if self._broke:
            f |= heads_cc.FLAG_BROKE
        if self._quiet():
            f |= heads_cc.FLAG_QUIET
        elif self._stuck():
            f |= heads_cc.FLAG_STUCK
        if self._lit_low():
            f |= heads_cc.FLAG_LIT_LOW
        if self.ch is not None and self.ch == ch:
            f |= heads_cc.FLAG_SCOPE
        return f

    def _values(self, ch):
        h = self.host
        thr = h.refresher.target.get(ch)
        try:
            drv = int(h.bells[ch].laser.duty_cycle)
        except Exception:
            drv = None
        return thr, drv

    def _env(self, typ, body):
        d = self._doc
        return {"schema": SCHEMA, "type": typ, "src": d["src"], "id": d["id"],
                "boot_id": d["boot_id"], "tick_ms": self.clock(), "body": body}

    def _ack(self, cid, what, ch, ok, why):
        thr = drv = None
        if ch is not None and 0 <= ch < self.host.n:
            thr, drv = self._values(ch)
        if cid == SERIAL_CID and self.cc is not None:
            self.cc.ack(None if ch is None else ch + 1, what, None if ok else (why or "bad"),
                        thr, drv, self._cc_flags(ch))
            return
        self.send_to(cid, self._env(T_ACK, {
            "what": what, "ch": None if ch is None else ch + 1, "ok": bool(ok),
            "why": why, "thr": thr, "drv": drv, "tower": self.host.tower.tower,
            "stuck": self._stuck(), "lit_low": self._lit_low()}))

    def _info(self, cid):
        h = self.host
        if cid == SERIAL_CID and self.cc is not None:
            return                  # the host has the kinds from the serial heartbeat
        s = h.tune.settings
        pt = getattr(h, "pt_level", None)
        self.send_to(cid, self._env(T_INFO, {
            "kinds": list(h.gate.kind), "active": self.active(),
            "scope": None if self.ch is None else self.ch + 1,
            "tower": h.tower.tower,
            "step": {"thr": s["threshold"]["step"], "drv": s["laser"]["step"]},
            "limits": {"thr": [s["threshold"]["min_duty"], s["threshold"]["max_duty"]],
                       "drv": [s["laser"]["min_duty"], s["laser"]["max_duty"]]},
            "frame_ms": SERIAL_FRAME_MS if cid == SERIAL_CID else FRAME_MS,
            "keepalive_ms": KEEPALIVE_MS,
            "pt": pt.info() if pt is not None else None}))

    def _frame(self, now):
        h = self.host
        ch = self.ch
        t = h.tune
        fresh = False
        if self.sweep is not None and self.sweep.ch == ch:
            fresh = True                             # the sweep measures this head
        elif self._adjustable():
            if self.last_measure is None or now - self.last_measure >= MEASURE_MS:
                self.last_measure = now
                fresh = t._measure(ch) is not False      # also reads the PT level
            else:
                fresh = True
        else:
            # strokes in progress: no mux, no laser mA; the PT level is its
            # own ADC pin (GP27, PT_N conversions, well under 1 ms), so it
            # stays live while a stepper or a ringer breaks the beam
            t.read_pt(ch)
        thr, drv = self._values(ch)
        pt = t.pt.get(ch)
        if self.cid == SERIAL_CID and self.cc is not None:
            if self.cc_alive is not None and not self.cc_alive():
                self._stop(None)                     # the host closed the data port
                return
            quiet = self._adjustable()
            f = self._cc_flags(ch) | (0 if fresh else heads_cc.FLAG_BUSY)
            self._broke = False
            self.cc.frame(ch + 1, f, thr, h.refresher.readback.get(ch), drv, t.ma.get(ch),
                          pt[0] if pt else None, pt[1] if pt else None, full=quiet)
            self.frames += 1
            return
        k, dt, gap = self.ring.window()
        b = self._body
        b["ch"] = ch + 1
        b["n"] = k
        b["dt"] = round(dt, 1)
        b["gap"] = gap
        b["bits"] = self.ring.hex_bits(k) if k else ""
        b["ma"] = t.ma.get(ch)                       # average mA (above dark)
        b["ma_on"] = t.ma_on.get(ch)                 # / duty: the current while on
        b["rb"] = h.refresher.readback.get(ch)
        b["thr"] = thr
        b["drv"] = drv
        b["pt"] = pt[0] if pt else None             # volts; null: not wired on this channel
        b["pt_span"] = pt[1] if pt else None
        b["tower"] = h.tower.tower
        b["stuck"] = self._stuck()
        b["lit_low"] = self._lit_low()          # held (bit 0) = light: heads.pt_light
        b["busy"] = not fresh
        self._doc["tick_ms"] = now
        got = self.send_to(self.cid, self._doc)
        if got is None:
            self._stop(None)                         # the client has gone
        elif got:
            self.frames += 1

    def status(self):
        return {"scope": None if self.ch is None else self.ch + 1,
                "frames": self.frames, "errors": self.errors,
                "dropped": self.dropped, "dead": self.dead}
