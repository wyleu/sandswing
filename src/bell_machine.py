"""
bell_machine.py
===============
Pure-Python state machine for one tower bell.

Filename: bell_machine.py
Runs on:  CPython (desktop tests) AND CircuitPython
          No `board`, `digitalio`, PWM, mux, Wi-Fi, or MIDI.

WHY THIS FILE EXISTS
    Lineup (sandswing.py) finds a safe laser duty and whether PT / I work.
    Detect (historic pico_circuitp_*latch*.py) scanned 8 GPIOs and emitted
    MIDI. That pin map does not match this board.

    The ringing-room need is *states*, not raw GPIO:

        IDLE
        STOOD            # up; hand vs back not yet known (typical at boot)
        STOOD_HAND       # stood on the forehand / handstroke side
        STOOD_BACK       # stood on the backstroke side
        MOVING           # left the rest band; direction not yet known
        MOVING_CW        # 7-unit strip decoded one way
        MOVING_ACW       # 7-unit strip decoded the other way
        FAULT            # lost sight, period violation, broken stay, etc.

    STRIKE is an *event* (clapper / learned delay), never a place to live.

OPTICS THIS MACHINE ASSUMES (it does not drive them)
    Wheel: 7-unit reflective strip
        2 reflect + 1 dark + 4 reflect
        Rest position = dark band → no return → wheel input False
        First shine after rest → MOVING
        Sequence of bands → CW vs ACW
    Stand: two further reflective strips
        one at the handstroke balance, one at backstroke
        live + wheel quiet → STOOD / STOOD_HAND / STOOD_BACK
    Laser duty is parked by the detect program, not by this module.

PERIOD WATCHDOG
    Each bell has a swing time T (settings, ms).
    No edge for ~1.5 T while MOVING_* → FAULT or IDLE ("lost sight").
    Edges much faster than T → FAULT (not a strike stream).

PUBLIC API
    BellMachine(name, t_ms=2500, lost_mult=1.5)
    boot(now_ms, wheel, stand_hand, stand_back) -> state
    update(now_ms, wheel, stand_hand, stand_back, pattern=None)
        -> (state, events)
    pattern is None | "cw" | "acw" once a *separate* strip decoder is sure.
    This file does not decode 2-1-4 itself (keep that next door).

DOES NOT
    Claim GPIO, run PWM, read the 4051, speak MIDI, or touch settings.json.
    Guess hand vs back at boot without a labelled stand strip.

TEST
    python3 tests/test_machine.py   # on the Pi, no Pico required
"""
# src/bell_machine.py


IDLE = "IDLE"
STOOD = "STOOD"
STOOD_HAND = "STOOD_HAND"
STOOD_BACK = "STOOD_BACK"
MOVING = "MOVING"
MOVING_CW = "MOVING_CW"
MOVING_ACW = "MOVING_ACW"
FAULT = "FAULT"

MOVING_STATES = (MOVING, MOVING_CW, MOVING_ACW)
STOOD_STATES = (STOOD, STOOD_HAND, STOOD_BACK)


def _stood_from_strips(stand_hand, stand_back):
    if stand_hand and stand_back:
        return STOOD
    if stand_hand:
        return STOOD_HAND
    if stand_back:
        return STOOD_BACK
    return None


class BellMachine:
    def __init__(self, name, t_ms=2500, lost_mult=1.5, min_period_frac=0.15):
        self.name = name
        self.t_ms = int(t_ms)
        self.lost_ms = int(t_ms * lost_mult)
        self.min_edge_ms = int(t_ms * min_period_frac)
        self.state = IDLE
        self.last_edge_ms = 0
        self.last_wheel = False
        self.events = []

    def boot(self, now_ms, wheel, stand_hand, stand_back):
        self.events = []
        self.last_wheel = bool(wheel)
        self.last_edge_ms = now_ms
        stood = _stood_from_strips(stand_hand, stand_back)
        if stood and not wheel:
            self.state = stood
            self.events.append("stood")
        elif wheel:
            self.state = MOVING
            self.events.append("started")
        else:
            self.state = IDLE
        return self.state

    def update(self, now_ms, wheel, stand_hand, stand_back, pattern=None):
        self.events = []
        wheel = bool(wheel)
        stand_hand = bool(stand_hand)
        stand_back = bool(stand_back)
        rising = wheel and not self.last_wheel
        self.last_wheel = wheel

        if rising:
            if self.last_edge_ms and (now_ms - self.last_edge_ms) < self.min_edge_ms:
                self.state = FAULT
                self.events.append("fault")
                self.last_edge_ms = now_ms
                return self.state, list(self.events)
            self.last_edge_ms = now_ms

        if self.state == FAULT:
            return self.state, list(self.events)

        stood = _stood_from_strips(stand_hand, stand_back)
        quiet = not wheel

        if stood and quiet:
            if self.state != stood:
                self.state = stood
                self.events.append("stood")
            return self.state, list(self.events)

        if rising and self.state in (IDLE,) + STOOD_STATES:
            self.state = MOVING
            self.events.append("started")
            return self.state, list(self.events)

        if self.state in MOVING_STATES:
            if pattern == "cw" and self.state != MOVING_CW:
                self.state = MOVING_CW
                self.events.append("direction")
            elif pattern == "acw" and self.state != MOVING_ACW:
                self.state = MOVING_ACW
                self.events.append("direction")
            if self.last_edge_ms and (now_ms - self.last_edge_ms) > self.lost_ms:
                self.state = FAULT
                self.events.append("lost")
            return self.state, list(self.events)

        if quiet and not stood:
            if self.state != IDLE:
                self.state = IDLE
            return self.state, list(self.events)

        return self.state, list(self.events)