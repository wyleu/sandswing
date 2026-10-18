# Fake CircuitPython watchdog module.


class _Mode:
    def __init__(self, n):
        self.n = n

    def __str__(self):
        return self.n


class WatchDogMode:
    RAISE = _Mode("RAISE")
    RESET = _Mode("RESET")
