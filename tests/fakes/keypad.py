# Fake CircuitPython keypad: Keys / Event / EventQueue.
# Keys claims its pins like the real one ('GPxx in use' on a double claim).
# It "scans" whenever the queue is looked at: each pin's level comes from
# _fake_hw.din_fn (else the idle pull level), a change is queued with
# timestamp = supervisor.ticks_ms() (patch that to drive time).  Tests can
# also queue events directly with Keys.inject(key, pressed, timestamp).
import _fake_hw

made = []          # every Keys built (tests look at the last one)


class Event:
    def __init__(self, key_number=0, pressed=True, timestamp=None):
        if timestamp is None:
            import supervisor
            timestamp = supervisor.ticks_ms()
        self.key_number, self.pressed, self.timestamp = key_number, pressed, timestamp

    @property
    def released(self):
        return not self.pressed

    def __eq__(self, other):
        return (self.key_number, self.pressed) == (other.key_number, other.pressed)

    def __hash__(self):
        return hash((self.key_number, self.pressed))


class EventQueue:
    def __init__(self, scanner, max_events):
        self._scanner = scanner
        self._q = []
        self._max = max_events
        self.overflowed = False

    def _put(self, key, pressed, ts):
        if len(self._q) >= self._max:
            self.overflowed = True       # new event discarded
            return
        self._q.append((key, pressed, ts))

    def get_into(self, ev):
        self._scanner.scan()
        if not self._q:
            return False
        ev.key_number, ev.pressed, ev.timestamp = self._q.pop(0)
        return True

    def get(self):
        ev = Event(0, True, 0)
        return ev if self.get_into(ev) else None

    def clear(self):
        del self._q[:]
        self.overflowed = False

    def __len__(self):
        self._scanner.scan()
        return len(self._q)

    def __bool__(self):
        return len(self) > 0


class Keys(_fake_hw.Claimed):
    def __init__(self, pins, *, value_when_pressed, pull=True, interval=0.02,
                 max_events=64, debounce_threshold=1):
        self._claimed = []
        for p in pins:
            _fake_hw.claim(p, "keypad.Keys")
            self._claimed.append(p)
        self.pins = list(pins)
        self.value_when_pressed = value_when_pressed
        self.pull = pull
        self.interval = interval
        self.max_events = max_events
        self.key_count = len(pins)
        self.events = EventQueue(self, max_events)
        self.scanning = True             # False: inject() only, no pin scan
        self._state = [False] * len(pins)   # like the real scanner: all released
        made.append(self)

    def _level(self, gp):
        fn = _fake_hw.din_fn.get(gp)
        if fn is not None:
            return bool(fn())
        if self.pull:                    # idle at the pull level
            return not self.value_when_pressed
        return False

    def scan(self):
        if not self.scanning or self._claimed is None:
            return
        import supervisor
        for k, p in enumerate(self.pins):
            pressed = self._level(p.gp) == self.value_when_pressed
            if pressed != self._state[k]:
                self._state[k] = pressed
                self.events._put(k, pressed, supervisor.ticks_ms())

    def inject(self, key, pressed, timestamp):
        self.events._put(key, pressed, timestamp)

    def reset(self):
        self._state = [False] * len(self.pins)

    def deinit(self):
        if self._claimed:
            for p in self._claimed:
                _fake_hw.release(p)
        self._claimed = None
