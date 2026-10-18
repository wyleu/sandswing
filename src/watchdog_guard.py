# watchdog_guard.py -- hardware watchdog for long unattended runs. OFF by
# default.  One object; concentrator.py arms it after startup and feeds it
# from ONE place (the steady sense loop).
#
#   settings "watchdog": {"enabled": false, "timeout_s": 8, "mode": "reset",
#                         "allow_with_usb": false,
#                         "heartbeats": {"ws_poll": 5}, "ack_after_s": 86400}
#   (settings "test.enter": "watchdog" is unrelated and ignored.)
#
# Never arms when: enabled is false; a file /nowatchdog exists on CIRCUITPY;
# the USB serial console is connected and allow_with_usb is false; or the
# board has no watchdog module.  The reason is printed and in status().
#
# Real CircuitPython 10 behaviour on RP2040/RP2350 (from the port source):
#   * timeout is checked as an int <= 8 (hardware max 8.388 s): we clamp.
#   * only WatchDogMode.RESET exists here (RAISE -> NotImplementedError).
#   * mode = None really disables it (clears the hardware enable bit);
#     there is no deinit() on WatchDogTimer in CP10.
#   * when code.py ends by Ctrl-C (KeyboardInterrupt), an auto-reload or
#     a normal return, the supervisor disables it before the REPL.  After
#     an UNHANDLED EXCEPTION it stays armed, so the board resets within
#     timeout_s while you read the traceback.  That is what /nowatchdog
#     and the USB rule are for.
import time

MAX_TIMEOUT_S = 8          # CircuitPython RP2 limit (8.388 s rounded down)
MIN_TIMEOUT_S = 1
FLAG_FILE = "/nowatchdog"


def _hw():
    """(watchdog object, WatchDogMode.RESET) or (None, None)."""
    try:
        from microcontroller import watchdog
        from watchdog import WatchDogMode
        return watchdog, WatchDogMode.RESET
    except (ImportError, AttributeError):
        return None, None


def _serial_connected():
    try:
        import supervisor
        return bool(supervisor.runtime.serial_connected)
    except (ImportError, AttributeError):
        return False


def _exists(path):
    try:
        import os
        os.stat(path)
        return True
    except OSError:
        return False


def _name(enum_value):
    """microcontroller.ResetReason.WATCHDOG -> "WATCHDOG"."""
    if enum_value is None:
        return None
    return str(enum_value).split(".")[-1]


def boot_reasons():
    """(reset_reason, run_reason) as plain names, None if unknown."""
    reset = run = None
    try:
        import microcontroller
        reset = _name(microcontroller.cpu.reset_reason)
    except (ImportError, AttributeError):
        pass
    try:
        import supervisor
        run = _name(supervisor.runtime.run_reason)
    except (ImportError, AttributeError):
        pass
    return reset, run


def clamp_timeout(t):
    t = int(float(t))
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, t))


class _Paused:
    def __init__(self, guard):
        self.guard = guard

    def __enter__(self):
        self.guard.pause()
        return self.guard

    def __exit__(self, et, ev, tb):
        self.guard.resume()
        return False


class WatchdogGuard:
    def __init__(self, settings=None, hw=None, serial_fn=None, exists_fn=None,
                 clock=None, reasons=None):
        s = settings or {}
        self.enabled = bool(s.get("enabled", False))
        self.timeout_s = clamp_timeout(s.get("timeout_s", MAX_TIMEOUT_S))
        self.mode_name = str(s.get("mode", "reset")).lower()
        self.allow_with_usb = bool(s.get("allow_with_usb", False))
        self.max_age = dict((k, float(v)) for k, v in (s.get("heartbeats") or {}).items())
        self.ack_after_s = float(s.get("ack_after_s", 86400))
        self._hw = hw                       # (wdt, RESET) for tests; else lazy
        self._serial = serial_fn or _serial_connected
        self._exists = exists_fn or _exists
        self.clock = clock or time.monotonic
        self.last_reset, self.run_reason = reasons or boot_reasons()
        self.boot_at = self.clock()
        self.show_reset = self.last_reset == "WATCHDOG"
        self.armed = False
        self.paused = False
        self.why = "not armed yet (startup)"
        self.beats = {}
        self.feeds = 0
        self.missed = 0
        self.stale = []

    # ---- arming ---------------------------------------------------------
    def _wdt(self):
        if self._hw is None:
            self._hw = _hw()
        return self._hw

    def arm(self):
        """Call once, just before the steady loop. Returns True if armed."""
        wdt, reset = self._wdt()
        why = None
        if not self.enabled:
            why = "disabled in settings (watchdog.enabled false)"
        elif self._exists(FLAG_FILE):
            why = "%s exists on CIRCUITPY" % FLAG_FILE
        elif self._serial() and not self.allow_with_usb:
            why = "USB console connected (watchdog.allow_with_usb false)"
        elif self.mode_name != "reset":
            why = "mode %r not supported (only 'reset' on RP2)" % self.mode_name
        elif wdt is None:
            why = "no watchdog module on this board"
        if why is not None:
            self.why = why
            print("watchdog NOT armed:", why)
            return False
        wdt.timeout = self.timeout_s           # setting the timeout also feeds
        wdt.mode = reset
        self.armed = True
        self.why = "armed"
        now = self.clock()
        for k in self.max_age:
            self.beats.setdefault(k, now)      # grace: count from arming
        print("WATCHDOG ARMED: reset after %d s without a feed. Ctrl-C to the "
              "REPL disarms it; an unhandled exception does NOT (board resets). "
              "Create %s to stop it arming." % (self.timeout_s, FLAG_FILE))
        return True

    def pause(self):
        """Disable around long blocking work (rescan/survey)."""
        if self.armed and not self.paused:
            self._wdt()[0].mode = None
            self.paused = True

    def resume(self):
        if self.armed and self.paused:
            wdt, reset = self._wdt()
            wdt.timeout = self.timeout_s
            wdt.mode = reset
            self.paused = False

    def paused_for(self):
        """with guard.paused_for(): long_blocking_work()"""
        return _Paused(self)

    def disarm(self, reason="exit"):
        """Clean exit / Ctrl-C: really off (mode None)."""
        if self.armed:
            wdt = self._wdt()[0]
            wdt.mode = None
            if hasattr(wdt, "deinit"):
                wdt.deinit()
            print("watchdog disarmed:", reason)
        self.armed = False
        self.paused = False
        self.why = "disarmed (%s)" % reason

    # ---- feeding --------------------------------------------------------
    def checkin(self, name, now=None):
        """Other tasks call this each pass (e.g. ws_poll)."""
        self.beats[name] = self.clock() if now is None else now

    def loop_tick(self, now=None):
        """The ONE feed point: call once per completed steady-loop pass.
        Feeds only if every configured heartbeat is fresh."""
        now = self.clock() if now is None else now
        self.stale = [k for k, age in self.max_age.items()
                      if now - self.beats.get(k, -1e9) > age]
        if self.show_reset and now - self.boot_at >= self.ack_after_s:
            self.show_reset = False           # a clean day since the reset
        if not self.armed or self.paused:
            return False
        if self.stale:
            self.missed += 1
            return False
        self._wdt()[0].feed()
        self.feeds += 1
        return True

    # ---- reporting ------------------------------------------------------
    def acknowledge(self):
        self.show_reset = False

    def panel_state(self):
        return "watchdog_reset" if self.show_reset else None

    def status(self):
        now = self.clock()
        return {"enabled": self.enabled, "armed": self.armed,
                "paused": self.paused, "why": self.why,
                "timeout_s": self.timeout_s, "last_reset": self.last_reset,
                "run_reason": self.run_reason, "show_reset": self.show_reset,
                "feeds": self.feeds, "missed": self.missed,
                "stale": list(self.stale),
                "beat_age_s": dict((k, round(now - v, 2)) for k, v in self.beats.items())}
