# sntp_pico.py -- millisecond SNTP client for the sandswing Pico 2 W
#
# CircuitPython 10 compatible (socketpool, struct, supervisor.ticks_ms).
# Plain CPython runs it too when socketpool/supervisor fakes are injected.
#
#   import socketpool, wifi
#   from sntp_pico import TickClock, SntpClock
#   clock = TickClock()                       # supervisor.ticks_ms, unwrapped
#   sntp = SntpClock(socketpool.SocketPool(wifi.radio), ["192.168.1.40"],
#                    clock=clock)
#   asyncio.create_task(sntp.run())           # bursts every interval_s
#   ...
#   t = clock.now()                           # stamp an edge (extended ms)
#   sntp.utc_ms(t)  -> int ms since 1970, or None while unsynced
#   sntp.error_ms(t) -> error bound in ms, or None
#   sntp.state()    -> "unsynced" | "synced" | "stale" (holdover) | "held"
#   sntp.set_ringing(True/False)  # from TowerWatch.tower (active/ringing_down)
#   sntp.accuracy_class(t) -> "ms" | "s" | None
#
# Two views of the clock:
#   free     tower inactive: bursts update the estimate, and the ISSUED
#            mapping follows it by slewing (never a backward step).
#   ringing  tower active / ringing_down: the issued mapping is frozen at
#            session start (one straight line: pure function of tick, constant
#            rate).  Lock loss/regain does nothing to it.  Samples may still be
#            taken (ring_sample) but only refine the estimate.
# Leaving ringing applies the estimate by the rule in _apply():
#   forward correction > step_ms -> step forward (new epoch, logged);
#   otherwise (any backward, small forward) -> slew at max_slew_ppm.
# utc_ms is therefore a non-decreasing function of tick, and stamp() also
# guards against any decrease in issue order.
#
# Island site: servers are the local GPS boxes (sandgps*.local) by IP only.
# No internet pool or fallback exists anywhere.  When GPS is lost the last
# offset/drift are kept (holdover) and error_ms() grows with age.
#
# Why custom: adafruit_ntp returns whole seconds.  Here t1/t4 come from
# supervisor.ticks_ms (1 ms) and t2/t3 from the server's 64-bit NTP stamps.
# Never use time.monotonic() for event times (float, loses ms after days).
#
# Integer time: CircuitPython floats are 32-bit (24-bit mantissa), so a Unix
# time in ms (~1.8e12) held as a float is quantised to 2**17 = 131072 ms.
# Ticks, utc, offsets and mapping bases are therefore Python ints (arbitrary
# precision) end to end.  Floats (drift / slew rates, fit intercept) only
# ever multiply SMALL deltas (ms since a reference tick), and the product is
# rounded to int before it is added to an int base.

import struct
import time

from time_servers import normalise
from time_select import choose, STRATUM_MIN, STRATUM_MAX

try:
    import asyncio
except ImportError:
    asyncio = None

TICKS_PERIOD = 1 << 29          # supervisor.ticks_ms wraps here (~6.2 days)
TICKS_MASK = TICKS_PERIOD - 1
TICKS_HALF = TICKS_PERIOD >> 1
NTP_UNIX_DELTA_S = 2208988800   # 1900-01-01 -> 1970-01-01
NTP_PORT = 123
NTP_FRAC_BITS = 32              # NTP timestamps are 32.32 fixed point
NTP_FRAC_HALF = 1 << (NTP_FRAC_BITS - 1)
MS_PER_S = 1000
US_PER_MS = 1000
TICK_MS = 1                     # supervisor.ticks_ms resolution
# Offset noise of one sample from tick quantisation (t1 and t4 each +-1 tick).
# A drift slope is fitted only once the samples span long enough for that
# noise to be below the crystal tolerance (FIT_NOISE_MS / xtal_ppm, 40 s at
# 50 ppm); before that the drift is taken as 0 (within the crystal spec).
FIT_NOISE_MS = 2 * TICK_MS
# accuracy_class() buckets (error bound in ms): "ms" = bell timing,
# "s" = tower-clock check (rounds to the right second), else None.
ACC_MS_MAX_MS = 10.0
ACC_S_MAX_MS = 500.0
# Server choice: one short probe of each candidate before a free-mode burst.
PROBE_TIMEOUT_S = 0.1           # per probe; a dead server costs no more
BOOT_RETRY_S = 15               # retry interval while unsynced (not interval_s)


def _default_ticks():
    import supervisor
    return supervisor.ticks_ms()


class TickClock:
    """supervisor.ticks_ms extended past its 2**29 wrap.

    now() must be called at least once per ~3.1 days (half a wrap); the
    SNTP loop does that.  extend(raw) maps a raw tick taken at most half a
    wrap before the latest now() onto the same extended scale."""

    def __init__(self, ticks_fn=None):
        self._ticks = ticks_fn or _default_ticks
        self._last_raw = self._ticks() & TICKS_MASK
        self._last_ext = self._last_raw
        self.wraps = 0

    def extend(self, raw):
        raw = int(raw) & TICKS_MASK
        d = (raw - self._last_raw) & TICKS_MASK
        if d < TICKS_HALF:                       # forward (or same)
            ext = self._last_ext + d
            if raw < self._last_raw:
                self.wraps += 1
            self._last_raw, self._last_ext = raw, ext
            return ext
        back = (self._last_raw - raw) & TICKS_MASK
        return self._last_ext - back             # an older stamp

    def now(self):
        return self.extend(self._ticks())


def _ntp_to_unix(sec, frac, per_s):
    """NTP 32.32 fixed point -> Unix time in 1/per_s s (int, rounded)."""
    return ((sec - NTP_UNIX_DELTA_S) * per_s
            + ((frac * per_s + NTP_FRAC_HALF) >> NTP_FRAC_BITS))


def ntp_to_unix_ms(sec, frac):
    """NTP 32.32 fixed point -> Unix ms (int)."""
    return _ntp_to_unix(sec, frac, MS_PER_S)


def ntp_to_unix_us(sec, frac):
    """NTP 32.32 fixed point -> Unix us (int); keeps the sub-ms part."""
    return _ntp_to_unix(sec, frac, MS_PER_S * US_PER_MS)


def us_to_ms(us):
    """Int us -> int ms, rounded half up (integer math only)."""
    return (us + US_PER_MS // 2) // US_PER_MS


def offset_delay(t1, t2, t3, t4):
    """Classic NTP: offset = ((t2-t1)+(t3-t4))/2, rtt = (t4-t1)-(t3-t2).
    All four in the same integer unit (local t1/t4, server t2/t3).  offset
    is server minus local (int, floor of the half unit)."""
    return ((t2 - t1) + (t3 - t4)) // 2, (t4 - t1) - (t3 - t2)


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


class SntpClock:
    """Burst SNTP against fixed IPs, min-RTT selection, drift fit.

    Settings (all optional; see the spec, "Time and timestamps"):
      burst          queries per burst                     (8)
      interval_s     seconds between bursts                (300)
      gap_s          pause between queries in a burst      (0.05)
      timeout_s      socket timeout per query; the most a
                     query can stall the caller            (0.25)
      stale_after    missed bursts before "stale"          (3)
      rtt_k          outlier if rtt > median + k*MAD of
                     recent accepted burst minima          (3.0)
      rtt_hist       burst minima kept for that test       (16)
      fit_samples    good samples used for the drift fit   (8)
      xtal_ppm       crystal tolerance: drift uncertainty for
                     the error bound until >= 3 samples give
                     a slope standard error (3 sigma, capped) (50)
      hold_ppm       floor on that uncertainty: crystal wander
                     (temperature) the fit cannot see, so the
                     holdover bound keeps growing            (5)
      max_slew_ppm   slew rate when applying a correction; 500 ppm
                     distorts a 2 s stroke interval by <= 1 ms (500)
      step_ms        forward corrections above this step instead
                     of slewing (new epoch)                   (100)
      ring_sample    take diagnostic samples while ringing  (False)
      ring_burst     queries per diagnostic burst             (2)
      ring_interval_s  seconds between diagnostic bursts    (900)
    """

    def __init__(self, pool, servers, clock=None, port=NTP_PORT, burst=8,
                 interval_s=300, gap_s=0.05, timeout_s=0.25, stale_after=3,
                 rtt_k=3.0, rtt_hist=16, fit_samples=8, xtal_ppm=50.0,
                 hold_ppm=5.0, max_slew_ppm=500.0, step_ms=100.0,
                 ring_sample=False, ring_burst=2, ring_interval_s=900,
                 sleep_fn=None):
        self.pool = pool
        self.servers = normalise(servers)     # a bare string is one IP
        self.server = None      # chosen server (probe), bursts go only here
        self.stratum = None     # its stratum
        self.disagree = False   # two valid servers > SERVER_DISAGREE_MS apart
        self.clock = clock or TickClock()
        self.port = int(port)
        self.burst = int(burst)
        self.interval_s = float(interval_s)
        self.gap_s = float(gap_s)
        self.timeout_s = float(timeout_s)
        self.stale_after = int(stale_after)
        self.rtt_k = float(rtt_k)
        self.rtt_hist = int(rtt_hist)
        self.fit_samples = int(fit_samples)
        self.xtal_ppm = float(xtal_ppm)
        self.hold_ppm = float(hold_ppm)
        self.max_slew = float(max_slew_ppm) * 1e-6
        self.step_ms = float(step_ms)
        self.ring_sample = bool(ring_sample)
        self.ring_burst = int(ring_burst)
        self.ring_interval_s = float(ring_interval_s)
        self.segs = []          # issued mapping: (t0, u0, dr), t0 ascending;
                                # t0/u0 int ms, dr = rate - 1 (small float)
        self.epoch = 0          # +1 whenever the issued mapping changes
        self.session = 0        # +1 at every ringing start
        self.ringing = False
        self._session_null = False
        self._kick = False
        self._last_issue = None # (tick, utc) of the last stamp() for the guard
        self.guard_hits = 0
        self.events = []        # mapping changes, for the log
        self._sleep = sleep_fn or time.sleep
        self.samples = []       # accepted burst minima: (tick, offset, rtt)
        self.rtts = []          # recent accepted minima rtt, for outlier test
        self.a = None           # offset at tick tref (int us)
        self.a_frac = 0.0       # small float part of the fitted offset (ms)
        self.b = 0.0            # drift (ms per ms)
                                # utc_ms = tick + ms(a + us(a_frac + b*(tick-tref)))
        self.tref = 0
        self.resid = 0.0        # max |residual| of the fit, ms
        self.drift_unc = self.xtal_ppm * 1e-6   # ms/ms; fit narrows it (n >= 3)
        self.last_good = None   # extended tick of the last accepted burst
        self.stats = {"sent": 0, "ok": 0, "timeout": 0, "bad": 0,
                      "rejected_rtt": 0, "rejected_stratum": 0, "bursts": 0}
        self.last = None        # summary of the last burst (for the boot log)
        self._buf = bytearray(48)
        self._serial = 0

    # ---- one query --------------------------------------------------------
    def _query(self, ip, timeout_s=None):
        """One SNTP exchange. Returns (t1, t4, t2, t3, stratum) or None:
        t1/t4 extended ticks (ms), t2/t3 server Unix us (ints).
        Never raises for network trouble; blocks at most timeout_s."""
        pkt = bytearray(48)
        pkt[0] = 0x23                            # LI 0, VN 4, mode 3 (client)
        self._serial = (self._serial + 1) & 0xFFFFFFFF
        tag = struct.pack(">II", 0x5A5A0000 | (self._serial >> 16), self._serial)
        pkt[40:48] = tag                         # echoed back as originate
        sock = None
        try:
            sock = self.pool.socket(self.pool.AF_INET, self.pool.SOCK_DGRAM)
            sock.settimeout(self.timeout_s if timeout_s is None else timeout_s)
            t1 = self.clock.now()
            sock.sendto(pkt, (ip, self.port))
            self.stats["sent"] += 1
            n, _addr = sock.recvfrom_into(self._buf)
            t4 = self.clock.now()
        except OSError:
            self.stats["timeout"] += 1
            return None
        except Exception:
            self.stats["bad"] += 1
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        b = self._buf
        if n < 48 or (b[0] & 0x07) != 4 or bytes(b[24:32]) != bytes(tag):
            self.stats["bad"] += 1               # not our reply / not a server
            return None
        stratum = b[1]
        li = b[0] >> 6
        if stratum < STRATUM_MIN or stratum > STRATUM_MAX or li == 3:
            self.stats["rejected_stratum"] += 1  # kiss-o'-death / unsynced
            return None
        s2, f2, s3, f3 = struct.unpack(">IIII", bytes(b[32:48]))
        self.stats["ok"] += 1
        return (t1, t4, ntp_to_unix_us(s2, f2), ntp_to_unix_us(s3, f3), stratum)

    # ---- one burst --------------------------------------------------------
    def _rtt_limit(self):
        if len(self.rtts) < 3:
            return None                          # not enough history yet
        med = _median(self.rtts)
        mad = _median([abs(r - med) for r in self.rtts])
        return med + self.rtt_k * max(mad, 1.0)  # 1 ms = tick resolution

    # ---- server choice ----------------------------------------------------
    def _probe(self, ip):
        """One short query -> time_select result dict."""
        r = self._query(ip, min(self.timeout_s, PROBE_TIMEOUT_S))
        if r is None:
            return {"ip": ip, "stratum": None, "rtt_ms": None, "offset_ms": None}
        rtt, _tick, off, st, _ip = self._better(None, r, ip)
        return {"ip": ip, "stratum": st, "rtt_ms": rtt, "offset_ms": us_to_ms(off)}

    def _needs_probe(self):
        """Free mode only (never while ringing: the mapping is frozen), and
        only when there is a choice to make."""
        if len(self.servers) == 1 and self.server is None:
            self.server = self.servers[0]        # no choice: stratum from the burst
        return len(self.servers) > 1 and not self.ringing

    def _choose(self, results):
        best, self.disagree = choose(results)
        if best is not None:
            self._use(best["ip"], best["stratum"])

    def _use(self, ip, stratum):
        """Burst only against ip.  A change of server clears the fit and
        its RTT history (never mix two servers in one drift fit) and starts
        a new epoch; the issued mapping then slews/steps onto the new one."""
        if ip == self.server:
            self._set_stratum(stratum)
            return
        old, self.server, self.stratum = self.server, ip, None
        self._set_stratum(stratum)
        if old is None:
            return
        self.samples, self.rtts = [], []
        self.a, self.a_frac, self.b, self.resid = None, 0.0, 0.0, 0.0
        self.drift_unc = self.xtal_ppm * 1e-6
        self.epoch += 1
        self._log("server", self.clock.now(), server=ip, was=old)

    def _set_stratum(self, stratum):
        """Console line when the server or its stratum changes."""
        if stratum is not None and stratum != self.stratum:
            self.stratum = stratum
            print("%.3f time server %s stratum %d" % (time.monotonic(), self.server, stratum))

    def select_server(self, between=None):
        if self._needs_probe():
            res = []
            for ip in self.servers:
                res.append(self._probe(ip))
                if between is not None:
                    between()
            self._choose(res)

    async def aselect_server(self):
        if self._needs_probe():
            res = []
            for ip in self.servers:
                res.append(self._probe(ip))
                await asyncio.sleep(0)           # yield between probes
            self._choose(res)

    # ---- one burst --------------------------------------------------------
    def burst_once(self, between=None, n=None):
        """Run one burst synchronously (server chosen first in free mode).
        between(): optional callback run between queries (sync use).
        Returns True if a sample was accepted."""
        self.select_server(between)
        best = None
        n = self.burst if n is None else int(n)
        for i in range(n):
            best = self._burst_query(best)
            if i + 1 < n:
                if between is not None:
                    between()
                else:
                    self._sleep(self.gap_s)
        return self._finish(best)

    async def aburst_once(self, n=None):
        await self.aselect_server()
        best = None
        n = self.burst if n is None else int(n)
        for i in range(n):
            best = self._burst_query(best)
            if i + 1 < n:
                await asyncio.sleep(self.gap_s)
        return self._finish(best)

    def _burst_query(self, best):
        ip = self.server
        if ip is None:
            return best                          # no server answered a probe
        r = self._query(ip)
        return best if r is None else self._better(best, r, ip)

    def _better(self, best, r, ip):
        t1, t4, t2, t3, st = r
        off, rtt_us = offset_delay(t1 * US_PER_MS, t2, t3, t4 * US_PER_MS)
        rtt = max(rtt_us, 0) / float(US_PER_MS)  # small: float ms is fine
        cand = (rtt, t1 + (t4 - t1) // 2, off, st, ip)   # off: int us
        return cand if best is None or cand[0] < best[0] else best

    def _finish(self, best):
        self.stats["bursts"] += 1
        self.clock.now()                         # keep the wrap extension alive
        if best is None:
            self.last = {"ok": False, "why": "no reply"}
            return False
        rtt, tick, off, st, ip = best
        self._set_stratum(st)
        lim = self._rtt_limit()
        if lim is not None and rtt > lim:
            self.stats["rejected_rtt"] += 1
            self.last = {"ok": False, "why": "rtt %.1f > %.1f" % (rtt, lim)}
            return False
        self.rtts.append(rtt)
        self.rtts = self.rtts[-self.rtt_hist:]
        self.samples.append((tick, off, rtt))    # off in int us
        self.samples = self.samples[-self.fit_samples:]
        self._fit()
        self.last_good = self.clock.now()
        if not self.ringing:
            self._apply(self.last_good)
        self.last = {"ok": True, "server": ip, "stratum": st, "rtt_ms": rtt,
                     "offset_ms": us_to_ms(off), "drift_ppm": self.b * 1e6}
        return True

    def _fit(self):
        """Least squares offset(tick) = a + a_frac + b*(tick - tref),
        tref = last tick, a = last offset (int us).  The fit itself runs on
        small deltas from (tref, a) in float ms, so float32 is enough."""
        pts = self.samples
        self.tref = pts[-1][0]
        self.a = pts[-1][1]
        if len(pts) < 2:
            self.a_frac, self.b, self.resid = 0.0, 0.0, 0.0
            return
        xs = [p[0] - self.tref for p in pts]
        ys = [(p[1] - self.a) / float(US_PER_MS) for p in pts]
        n = float(len(pts))
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        span = self.tref - pts[0][0]
        if sxx <= 0 or span * self.xtal_ppm * 1e-6 < FIT_NOISE_MS:
            # too short to see drift (e.g. the two boot bursts ~1 s apart:
            # 2 ms of noise would read as ~2000 ppm): offset only
            self.a_frac, self.b = my, 0.0
            self.resid = max(abs(y - my) for y in ys)
            return
        self.b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        self.a_frac = my - self.b * mx
        res = [y - (self.a_frac + self.b * x) for x, y in zip(xs, ys)]
        self.resid = max(abs(r) for r in res)
        if len(pts) >= 3:                        # slope standard error
            se = (sum(r * r for r in res) / (n - 2) / sxx) ** 0.5
            self.drift_unc = min(max(3.0 * se, self.hold_ppm * 1e-6),
                                 self.xtal_ppm * 1e-6)

    # ---- mapping ----------------------------------------------------------
    def _est(self, tick):
        """The current ESTIMATE (fit), not what is issued.  Int ms."""
        small_us = int(round((self.a_frac + self.b * (tick - self.tref)) * US_PER_MS))
        return tick + us_to_ms(self.a + small_us)

    def _seg_at(self, tick):
        seg = None
        for sg in self.segs:
            if sg[0] <= tick:
                seg = sg
            else:
                break
        return seg if seg is not None else (self.segs[0] if self.segs else None)

    def _issued(self, tick):
        sg = self._seg_at(tick)
        if sg is None:
            return None
        t0, u0, dr = sg
        d = tick - t0
        return u0 + d + int(round(d * dr))       # int ms; float only on d

    def _log(self, kind, tick, **kw):
        e = {"kind": kind, "tick_ms": tick, "epoch": self.epoch,
             "session": self.session}
        e.update(kw)
        self.events.append(e)
        self.events = self.events[-32:]

    def _apply(self, now):
        """Move the issued mapping onto the estimate, never backwards."""
        if self.a is None:
            return
        e_now = self._est(now)
        dr = self.b
        if not self.segs:
            self.segs = [(now, e_now, dr)]
            self.epoch += 1
            self._log("install", now)
            return
        m_now = self._issued(now)
        c = e_now - m_now                        # + = issued is behind
        self.segs = [sg for sg in self.segs if sg[0] <= now]
        self.epoch += 1
        if c > self.step_ms:
            self.segs.append((now, e_now, dr))
            self._log("step", now, correction_ms=c)
        elif c == 0:
            self.segs.append((now, m_now, dr))
        else:
            dur = abs(c) / self.max_slew
            sdr = dr + (self.max_slew if c > 0 else -self.max_slew)
            end = int(round(dur))                # slew length, whole ticks
            self.segs.append((now, m_now, sdr))
            self.segs.append((now + end, m_now + end + int(round(end * sdr)), dr))
            self._log("slew", now, correction_ms=c, slew_s=dur / MS_PER_S)
        self.segs = self.segs[-16:]

    def set_ringing(self, ringing, now=None):
        """Tower active / ringing_down -> True; inactive -> False."""
        ringing = bool(ringing)
        if ringing == self.ringing:
            return None
        now = self.clock.now() if now is None else now
        if ringing:
            self.session += 1
            self.ringing = True
            if self.segs:
                f = self._issued(now)
                dr = self.b if self.a is not None else self._seg_at(now)[2]
                self.segs = [sg for sg in self.segs if sg[0] <= now]
                self.segs.append((now, f, dr))     # one straight line
                self.segs = self.segs[-16:]
                self.epoch += 1
                self._session_null = False
            else:
                self._session_null = True        # unsynced: null all session
            self._log("freeze", now, null=self._session_null)
        else:
            self.ringing = False
            self._session_null = False
            self._log("release", now)
            self._apply(now)
            self._kick = True                    # fresh burst soon
        return self.events[-1] if self.events else None

    # ---- public -----------------------------------------------------------
    def mode(self):
        return "ringing" if self.ringing else "free"

    def state(self, now=None):
        if self.a is None or not self.segs:
            return "unsynced"
        if self.ringing:
            return "held"                        # frozen on purpose
        now = self.clock.now() if now is None else now
        if now - self.last_good > self.stale_after * self.interval_s * MS_PER_S:
            return "stale"
        return "synced"

    def utc_ms(self, tick):
        """Extended tick -> Unix ms (int) from the ISSUED mapping, or None
        (unsynced, or a ringing session that started unsynced)."""
        if self.ringing and self._session_null:
            return None
        u = self._issued(tick)
        return None if u is None else int(u)

    def error_ms(self, tick):
        """Error bound (ms) for utc_ms(tick): half the chosen RTT, tick
        resolution, fit residual, drift uncertainty times age of the fit,
        plus any known gap between issued mapping and estimate (slew in
        progress, or a frozen session)."""
        if self.a is None or not self.segs or self.utc_ms(tick) is None:
            return None
        rtt = self.samples[-1][2]
        age = abs(tick - self.tref)
        gap = abs(self._est(tick) - self._issued(tick))     # int ms
        return rtt / 2.0 + TICK_MS + self.resid + age * self.drift_unc + gap

    def accuracy_class(self, tick, ms_target=ACC_MS_MAX_MS, s_target=ACC_S_MAX_MS):
        """"ms" (bell timing), "s" (tower-clock check, rounds to the right
        second) or None, from the current error bound.  Holdover (stale)
        keeps a class only while the growing bound still fits."""
        e = self.error_ms(tick)
        if e is None:
            return None
        if e <= ms_target:
            return "ms"
        if e <= s_target:
            return "s"
        return None

    def stamp(self, raw_tick=None):
        """Event fields: tick_ms (extended), utc_ms (or None), err_ms, sync."""
        t = self.clock.now() if raw_tick is None else self.clock.extend(raw_tick)
        u = self.utc_ms(t)
        if u is not None and self._last_issue is not None:
            lt, lu = self._last_issue
            if t >= lt and u < lu:               # never decrease (guard)
                u = lu
                self.guard_hits += 1
        if u is not None and (self._last_issue is None or t >= self._last_issue[0]):
            self._last_issue = (t, u)
        synced = u is not None and self.a is not None
        return {"tick_ms": t, "utc_ms": u, "err_ms": self.error_ms(t),
                "sync": self.state(t), "acc": self.accuracy_class(t),
                "epoch": self.epoch, "session": self.session,
                "mode": self.mode(),
                "srv": self.server if synced else None,
                "stratum": self.stratum if synced else None}

    def boot_record(self):
        return {"sync": self.state(), "mode": self.mode(),
                "epoch": self.epoch, "servers": self.servers,
                "srv": self.server, "stratum": self.stratum,
                "disagree": self.disagree,
                "last": self.last, "stats": dict(self.stats),
                "events": list(self.events[-4:])}

    async def run(self):
        """Forever: one burst, then wait interval_s.  Exceptions from the
        network never leave this loop."""
        while True:
            ringing = self.ringing
            try:
                if not ringing:
                    await self.aburst_once()
                elif self.ring_sample:            # diagnostics only
                    await self.aburst_once(self.ring_burst)
            except Exception as e:                # belt and braces
                self.stats["bad"] += 1
                self.last = {"ok": False, "why": repr(e)}
            wait = self._wait_s(ringing)
            # sleep in short slices so a ring end triggers a prompt burst
            waited = 0.0
            slice_s = min(1.0, wait)
            while waited < wait:
                if self._kick:
                    self._kick = False
                    break
                await asyncio.sleep(slice_s)
                waited += slice_s

    def _wait_s(self, ringing):
        """Seconds to the next burst: BOOT_RETRY_S while unsynced (free)."""
        if ringing:
            return self.ring_interval_s if self.ring_sample else self.interval_s
        if self.state() == "unsynced":
            return min(BOOT_RETRY_S, self.interval_s)   # get a lock soon
        return self.interval_s

    @staticmethod
    def worst_case_ms(hours, ppm):
        """Absolute error added by an uncorrected drift uncertainty."""
        return hours * 3600.0 * 1000.0 * ppm * 1e-6
