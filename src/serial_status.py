"""
serial_status.py
================
The heartbeat, one line of JSON at a time, on the Pico's USB data port
(usb_cdc.data, enabled in boot.py; /dev/ttyACM1 on the Sandbells Pi, the
REPL console being ttyACM0).  No Wi-Fi: Sandbells' ingest_serial reads it
and fills the sandswing status page from it.

The line is the slim UDP heartbeat (link_health.udp_heartbeat_doc, the
same fields Sandbells already knows) plus a few memory / sense counters
(SERIAL_BODY_EXTRA) and this writer's own counters ("serial").  One per
serial_status.period_s, '\\n' terminated.

Never in the way of the strokes:
  * nothing is built while no host has the port open (connected False);
  * writes are write_timeout 0: the USB FIFO takes what it can, the rest
    goes on SERIAL_PUSH_S later; a line still not out when the next is due
    is given up (the next starts on a fresh line, Sandbells drops the cut one);
  * a line over SERIAL_LINE_MAX_B is not sent;
  * step() never raises; no usb_cdc / no data port / off in settings:
    open_serial_status() returns None and nothing runs.

LED echo (serial_status.leds.enabled): the front panel as it is painted,
so Sandbells can mirror it within a fraction of a second, not every 10 s:

    {"t":"led","n":12,"p":[[r,g,b] x6],"s":[state x6]}

LedEcho is its state model.  concentrator._paint hands it the frame's
arguments every paint (a compare, nothing built); on a change one line goes
out, at most one per SERIAL_LED_MIN_S, so the first state after a send is
the one sent and a short flash (a stroke's white) is held on the wire for
at least that long instead of being lost.  It is the ON phase of the panel
(as the heartbeat's "panel"): blinking is not sent.  The line waits while
a heartbeat line is part-written, is not started when the USB buffer takes
none of it (counted dropped, tried again SERIAL_LED_MIN_S later), and
nothing is built while no host has the port open; a host opening it gets
the current state.  Counters: the heartbeat line's "serial"."leds".

settings.json (optional; these are the defaults):

    "serial_status": { "enabled": true, "period_s": 10, "leds": {"enabled": true} }
"""

import time

from link_health import udp_heartbeat_doc, dumps_compact, _pick

SERIAL_PERIOD_S = 10.0         # a line this often (settings serial_status.period_s)
SERIAL_PERIOD_MIN_S = 2.0      # never more often than this
SERIAL_PUSH_S = 0.05           # a part-written line: push the rest this often
SERIAL_LINE_MAX_B = 2048       # bigger than this: not sent (counted as dropped)
SERIAL_ERR_CHARS = 40          # last error kept, cut to this
SERIAL_VIA = "usb_cdc"         # the line's "via": Sandbells knows it came by serial
# heartbeat body fields the UDP heartbeat leaves out (memory, sense queue)
SERIAL_BODY_EXTRA = ("mem_free", "gc_heap_kb", "edges_lost", "late_drains",
                     "strokes_undelivered", "watchdog")
NEWLINE = b"\n"
SERIAL_LED_MIN_S = 0.25        # LED echo: at most one line per this; a state shows at least this long
SERIAL_LED_CHECK_S = 0.05      # LED echo on and a host there: look for a change this often (= panel_task)
SERIAL_LED_TYPE = "led"        # the LED line's "t"


def serial_settings(cfg):
    """-> (enabled, period_s) from cfg "serial_status"; bad values: defaults."""
    sec = cfg.get("serial_status") if hasattr(cfg, "get") else None
    sec = sec if isinstance(sec, dict) else {}
    on = sec.get("enabled", True)
    on = on if isinstance(on, bool) else True
    try:
        period = float(sec.get("period_s", SERIAL_PERIOD_S))
    except (TypeError, ValueError):
        period = SERIAL_PERIOD_S
    if period != period:                      # NaN
        period = SERIAL_PERIOD_S
    return on, max(SERIAL_PERIOD_MIN_S, period)


def led_settings(cfg):
    """-> True if serial_status.leds.enabled (default True); bad values: True."""
    sec = cfg.get("serial_status") if hasattr(cfg, "get") else None
    sec = sec if isinstance(sec, dict) else {}
    leds = sec.get("leds")
    leds = leds if isinstance(leds, dict) else {}
    on = leds.get("enabled", True)
    return on if isinstance(on, bool) else True


class LedEcho:
    """The panel's state for the LED line.  render(key) -> (panel rgb
    lists, panel_state list) of a frame key (concentrator: _frame_args)."""

    def __init__(self, render, min_s=SERIAL_LED_MIN_S):
        self.render = render
        self.min_s = min_s
        self.lines = 0           # LED lines started
        self.dropped = 0         # USB buffer full: not started
        self._seen = None        # the newest key painted
        self._held = None        # the first key since the last send, not yet sent
        self._sent = None
        self._sent_at = None

    def painted(self, key):
        """Every paint: cheap (a compare), never raises."""
        try:
            if key == self._seen:
                return
            self._seen = key
            if self._held is None and key != self._sent:
                self._held = key
        except Exception:
            pass

    def resend(self):
        """A host (re)opened the port: send the current state."""
        self._sent = None
        self._held = self._seen

    def due(self, now):
        return self._held is not None and (
            self._sent_at is None or now - self._sent_at >= self.min_s)

    def line(self, dumps):
        p, st = self.render(self._held)
        return dumps({"t": SERIAL_LED_TYPE, "n": self.lines + 1, "p": p, "s": st})

    def done(self, now, sent):
        """After a try at the line: sent (started) or dropped."""
        self._sent_at = now
        if not sent:
            self.dropped += 1
            return
        self.lines += 1
        self._sent = self._held
        self._held = self._seen if self._seen != self._sent else None

    def counters(self):
        return {"lines": self.lines, "dropped": self.dropped, "min_s": self.min_s}


class SerialStatus:
    """port: usb_cdc.data (or a fake with write / connected).  build: () ->
    a heartbeat_doc.  step(now) does the work and says how long to sleep."""

    def __init__(self, port, build, period_s=SERIAL_PERIOD_S, dumps=dumps_compact,
                 max_b=SERIAL_LINE_MAX_B, now=None, leds=None):
        self.port = port
        self.leds = leds         # LedEcho or None (off)
        self._host = False       # a host had the port open at the last step
        self.build = build
        self.period_s = period_s
        self.dumps = dumps
        self.max_b = max_b
        try:
            port.write_timeout = 0            # never wait for the host
        except Exception:
            pass
        self.lines = 0           # whole lines written
        self.unread = 0          # due while no host had the port open (not built)
        self.dropped = 0         # too big
        self.cut = 0             # given up part-written
        self.errors = 0          # build / write raised
        self.last_error = None
        self._buf = None         # memoryview of the line being written
        self._off = 0
        self._fresh = False      # the next line starts with a newline (after a cut)
        self._due = time.monotonic() if now is None else now

    def _connected(self):
        c = getattr(self.port, "connected", True)
        return bool(c)

    def counters(self):
        return {"period_s": self.period_s, "lines": self.lines, "unread": self.unread,
                "dropped": self.dropped, "cut": self.cut, "errors": self.errors,
                "last_error": self.last_error,
                "leds": None if self.leds is None else self.leds.counters()}

    def line(self):
        """The line's text (no newline): slim heartbeat + extras + counters."""
        doc = self.build()
        slim = udp_heartbeat_doc(doc)
        body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
        slim["body"].update(_pick(body, SERIAL_BODY_EXTRA))
        slim["via"] = SERIAL_VIA
        slim["serial"] = self.counters()
        return self.dumps(slim)

    def _give_up(self):
        if self._buf is not None:
            self.cut += 1
        self._buf = None
        self._off = 0
        self._fresh = True

    def _push(self):
        n = self.port.write(self._buf[self._off:])
        self._off += n or 0
        if self._off >= len(self._buf):
            self._buf = None
            self._off = 0
            self.lines += 1

    def _start(self, data):
        """A line into the in-flight buffer and its first write."""
        self._buf = memoryview((NEWLINE if self._fresh else b"") + data + NEWLINE)
        self._fresh = False
        self._push()

    def _led_step(self, now):
        """The LED line, when one is due and nothing else is in flight."""
        data = self.leds.line(self.dumps).encode("utf-8")
        head = (NEWLINE if self._fresh else b"") + data + NEWLINE
        n = self.port.write(head) or 0
        if n <= 0:
            self.leds.done(now, False)        # buffer full: not started, not cut
            return
        self.leds.done(now, True)
        self._fresh = False
        if n < len(head):
            self._buf, self._off = memoryview(head), n
        else:
            self.lines += 1

    def step(self, now):
        """One turn: push a part-written line, or make the next one when
        due (heartbeat first, then the LED line).  -> seconds until the
        next turn.  Never raises."""
        host = False
        try:
            host = self._connected()
            if self._buf is not None:
                if not host or now >= self._due:
                    self._give_up()
                else:
                    self._push()
            if self._buf is None and now >= self._due:
                self._due = now + self.period_s
                if not host:
                    self.unread += 1
                    self._fresh = True        # whatever was in the FIFO is the host's past
                else:
                    data = self.line().encode("utf-8")
                    if len(data) > self.max_b:
                        self.dropped += 1
                    else:
                        self._start(data)
            if self.leds is not None and host:
                if not self._host:
                    self.leds.resend()        # a host just opened the port
                if self._buf is None and self.leds.due(now):
                    self._led_step(now)
        except Exception as e:
            self.errors += 1
            self.last_error = repr(e)[:SERIAL_ERR_CHARS]
            self._buf = None
            self._off = 0
            self._fresh = True
        self._host = host
        if self._buf is not None:
            return SERIAL_PUSH_S
        wait = max(0.0, self._due - now)
        if self.leds is not None and host:
            return min(wait, SERIAL_LED_CHECK_S)
        return wait


def open_serial_status(cfg, build, log=print, render=None):
    """SerialStatus on usb_cdc.data per cfg, or None (off, no usb_cdc, no
    data port: boot.py did not enable it).  render: LedEcho's (key ->
    panel, states); None or serial_status.leds.enabled false: no LED line."""
    on, period = serial_settings(cfg)
    if not on:
        log("serial status: off (serial_status.enabled false)")
        return None
    try:
        import usb_cdc
        port = usb_cdc.data
    except (ImportError, AttributeError):
        return None
    if port is None:
        log("serial status: no usb_cdc data port (boot.py)")
        return None
    leds = LedEcho(render) if render is not None and led_settings(cfg) else None
    log("serial status: a heartbeat line every %d s on the USB data port%s"
        % (period, ", LED echo on" if leds else ""))
    return SerialStatus(port, build, period, leds=leds)
