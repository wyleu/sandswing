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

settings.json (optional; these are the defaults):

    "serial_status": { "enabled": true, "period_s": 10 }
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


class SerialStatus:
    """port: usb_cdc.data (or a fake with write / connected).  build: () ->
    a heartbeat_doc.  step(now) does the work and says how long to sleep."""

    def __init__(self, port, build, period_s=SERIAL_PERIOD_S, dumps=dumps_compact,
                 max_b=SERIAL_LINE_MAX_B, now=None):
        self.port = port
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
                "last_error": self.last_error}

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

    def step(self, now):
        """One turn: push a part-written line, or make the next one when
        due.  -> seconds until the next turn.  Never raises."""
        try:
            if self._buf is not None:
                if not self._connected() or now >= self._due:
                    self._give_up()
                else:
                    self._push()
            if self._buf is None and now >= self._due:
                self._due = now + self.period_s
                if not self._connected():
                    self.unread += 1
                    self._fresh = True        # whatever was in the FIFO is the host's past
                else:
                    data = self.line().encode("utf-8")
                    if len(data) > self.max_b:
                        self.dropped += 1
                    else:
                        self._buf = memoryview((NEWLINE if self._fresh else b"") + data + NEWLINE)
                        self._fresh = False
                        self._push()
        except Exception as e:
            self.errors += 1
            self.last_error = repr(e)[:SERIAL_ERR_CHARS]
            self._buf = None
            self._off = 0
            self._fresh = True
        if self._buf is not None:
            return SERIAL_PUSH_S
        return max(0.0, self._due - now)


def open_serial_status(cfg, build, log=print):
    """SerialStatus on usb_cdc.data per cfg, or None (off, no usb_cdc, no
    data port: boot.py did not enable it)."""
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
    log("serial status: a heartbeat line every %d s on the USB data port" % period)
    return SerialStatus(port, build, period)
