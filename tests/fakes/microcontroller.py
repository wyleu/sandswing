# Fake CircuitPython microcontroller: cpu.reset_reason and the watchdog.
import _fake_hw


class _Enum:
    def __init__(self, cls, name):
        self.cls, self.name = cls, name

    def __str__(self):
        return "microcontroller.%s.%s" % (self.cls, self.name)

    __repr__ = __str__


class ResetReason:
    pass


for _n in ("POWER_ON", "BROWNOUT", "SOFTWARE", "DEEP_SLEEP_ALARM",
           "RESET_PIN", "WATCHDOG", "UNKNOWN", "RESCUE_DEBUG"):
    setattr(ResetReason, _n, _Enum("ResetReason", _n))


class _Cpu:
    reset_reason = ResetReason.POWER_ON


cpu = _Cpu()


class WatchDogTimer:
    """Like CP10 on RP2: timeout int <= 8 (ValueError above), mode None
    disables, no deinit(); feed() is a no-op while mode is None."""
    def __init__(self):
        self.mode_ = None
        self._timeout = None
        self.feeds = 0

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, t):
        if int(t) > 8 or t < 0:
            raise ValueError("timeout must be 0-8")
        self._timeout = t
        _fake_hw.wdt_log.append(("timeout", t, len(_fake_hw.history)))

    @property
    def mode(self):
        return self.mode_

    @mode.setter
    def mode(self, m):
        if m is not None and str(m) == "RAISE":
            raise NotImplementedError()
        self.mode_ = m
        _fake_hw.wdt_log.append(("mode", None if m is None else str(m),
                                 len(_fake_hw.history)))

    def feed(self):
        if self.mode_ is not None:
            self.feeds += 1


watchdog = WatchDogTimer()


resets = []                 # reset() calls (the real one never returns)


def reset():
    resets.append(1)
