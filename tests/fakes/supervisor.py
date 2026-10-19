# Fake CircuitPython supervisor: runtime.serial_connected / run_reason.


class _Run:
    def __init__(self, n):
        self.n = n

    def __str__(self):
        return "supervisor.RunReason." + self.n


class RunReason:
    STARTUP = _Run("STARTUP")
    AUTO_RELOAD = _Run("AUTO_RELOAD")
    SUPERVISOR_RELOAD = _Run("SUPERVISOR_RELOAD")
    REPL_RELOAD = _Run("REPL_RELOAD")


class _Runtime:
    serial_connected = False
    run_reason = RunReason.STARTUP


runtime = _Runtime()



def ticks_ms():
    """Like CircuitPython: ms since boot, wrapping at 2**29."""
    import time
    return int(time.monotonic() * 1000) & ((1 << 29) - 1)