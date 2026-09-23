"""
test_machine.py
===============
Desktop tests for src/bell_machine.py

Filename: tests/test_machine.py
Runs on:  CPython on the Pi (or any PC). Not CircuitPython. No Pico.

WHY THIS FILE EXISTS
    BellMachine has no GPIO. If the states are wrong, the tower program
    will be wrong even when the optics are perfect. These tests feed
    fake wheel / stand-tape booleans and check state + events.

    No pytest. Run:

        cd ~/Code/Sandbells/sandswing
        python3 src/tests/test_machine.py

WHAT IS COVERED
    boot
        idle, stood hand, stood back, both tapes → STOOD, wheel → MOVING
    swing
        IDLE + wheel rising → MOVING + "started"
    stand tapes (tape ≠ stood)
        enter hand tape → TOP_HAND + "top"
        leave before stand_hold_ms → MOVING + "checked" (rang through)
        hold ≥ stand_hold_ms → STOOD_HAND + "stood"
        same three for the back tape
        hold − 1 ms must not stand
    leave stand
        STOOD_* + wheel → MOVING + "started"
    direction
        pattern "cw" / "acw" → MOVING_CW / MOVING_ACW + "direction"
    watchdog
        edges closer than min_edge_ms → FAULT + "fault"
        no edge for lost_ms while moving → FAULT + "lost"
        FAULT stays FAULT (no silent recover)

WHAT IS NOT COVERED
    2-1-4 strip decoding (that is the next module)
    GPIO, 4051, PWM, NeoPixel, MIDI, settings.json, sandswing lineup
    FakeBell / full-stroke timing scripts (optional later)

HOLD TIME
    Tests use stand_hold_ms=350 to match BellMachine default.
    Times in the stand tests are milliseconds on the same clock as now_ms.

LAYOUT
    This file adds src/ to sys.path so `from bell_machine import ...` works
    whether you run it as tests/test_machine.py or from the repo root.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bell_machine import (
    BellMachine,
    IDLE,
    STOOD,
    STOOD_HAND,
    STOOD_BACK,
    TOP_HAND,
    TOP_BACK,
    MOVING,
    MOVING_CW,
    MOVING_ACW,
    FAULT,
)

HOLD = 350
fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok ", name)
    else:
        fails.append(name)
        print("FAIL", name, detail)


def m(**kw):
    return BellMachine("treble", t_ms=2500, stand_hold_ms=HOLD, **kw)


def test_boot_idle():
    b = m()
    st = b.boot(0, False, False, False)
    check("boot idle", st == IDLE)


def test_boot_stood_hand():
    b = m()
    st = b.boot(0, False, True, False)
    check("boot stood hand", st == STOOD_HAND and "stood" in b.events)


def test_boot_stood_back():
    b = m()
    st = b.boot(0, False, False, True)
    check("boot stood back", st == STOOD_BACK)


def test_boot_both_tapes():
    b = m()
    st = b.boot(0, False, True, True)
    check("boot both tapes", st == STOOD)


def test_boot_moving():
    b = m()
    st = b.boot(0, True, False, False)
    check("boot moving", st == MOVING and "started" in b.events)


def test_idle_to_moving():
    b = m()
    b.boot(0, False, False, False)
    st, ev = b.update(100, True, False, False)
    check("idle→moving", st == MOVING and "started" in ev)


def test_through_hand_top():
    b = m()
    b.boot(0, False, False, False)
    b.update(50, True, False, False)
    st, ev = b.update(100, False, True, False)
    check("enter top hand", st == TOP_HAND and "top" in ev)
    st, ev = b.update(200, False, False, False)
    check("checked hand", st == MOVING and "checked" in ev)
    check("not stood after check", st != STOOD_HAND)


def test_stand_hand_after_hold():
    b = m()
    b.boot(0, False, False, False)
    b.update(50, True, False, False)
    b.update(100, False, True, False)
    st, ev = b.update(100 + HOLD - 10, False, True, False)
    check("still top before hold", st == TOP_HAND and "stood" not in ev)
    st, ev = b.update(100 + HOLD, False, True, False)
    check("stood hand after hold", st == STOOD_HAND and "stood" in ev)


def test_hold_too_short():
    b = m()
    b.boot(0, False, False, False)
    b.update(50, True, False, False)
    b.update(100, False, True, False)
    st, ev = b.update(100 + HOLD - 1, False, False, False)
    check("340ms not stood", st != STOOD_HAND and "checked" in ev)


def test_through_back_top():
    b = m()
    b.boot(0, False, False, False)
    b.update(50, True, False, False)
    st, ev = b.update(100, False, False, True)
    check("enter top back", st == TOP_BACK and "top" in ev)
    st, ev = b.update(200, False, False, False)
    check("checked back", st == MOVING and "checked" in ev)


def test_stand_back_after_hold():
    b = m()
    b.boot(0, False, False, False)
    b.update(50, True, False, False)
    b.update(100, False, False, True)
    st, ev = b.update(100 + HOLD, False, False, True)
    check("stood back after hold", st == STOOD_BACK and "stood" in ev)


def test_leave_stand_starts():
    b = m()
    b.boot(0, False, True, False)
    st, ev = b.update(80, True, False, False)
    check("leave stand", st == MOVING and "started" in ev)


def test_direction():
    b = m()
    b.boot(0, False, False, False)
    b.update(100, True, False, False)
    st, ev = b.update(400, True, False, False, pattern="cw")
    check("cw", st == MOVING_CW and "direction" in ev)
    st, ev = b.update(700, True, False, False, pattern="acw")
    check("acw", st == MOVING_ACW and "direction" in ev)


def test_fault_fast_edges():
    b = m()
    b.boot(0, False, False, False)
    b.update(100, True, False, False)
    b.update(110, False, False, False)
    st, ev = b.update(120, True, False, False)
    check("fast edge fault", st == FAULT and "fault" in ev)


def test_lost_sight():
    b = BellMachine("treble", t_ms=1000, lost_mult=1.5, stand_hold_ms=HOLD)
    b.boot(0, False, False, False)
    b.update(100, True, False, False)
    st, ev = b.update(100 + 1501, False, False, False)
    check("lost", st == FAULT and "lost" in ev)


def test_fault_stays():
    b = m()
    b.boot(0, False, False, False)
    b.update(100, True, False, False)
    b.update(110, False, False, False)
    b.update(120, True, False, False)
    st, ev = b.update(500, False, True, False)
    check("fault latched", st == FAULT and ev == [])


if __name__ == "__main__":
    for fn in (
        test_boot_idle,
        test_boot_stood_hand,
        test_boot_stood_back,
        test_boot_both_tapes,
        test_boot_moving,
        test_idle_to_moving,
        test_through_hand_top,
        test_stand_hand_after_hold,
        test_hold_too_short,
        test_through_back_top,
        test_stand_back_after_hold,
        test_leave_stand_starts,
        test_direction,
        test_fault_fast_edges,
        test_lost_sight,
        test_fault_stays,
    ):
        print(fn.__name__)
        fn()
    print()
    if fails:
        print("FAILED:", ", ".join(fails))
        sys.exit(1)
    print("all passed")