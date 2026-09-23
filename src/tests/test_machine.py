"""
test_machine.py
===============
CPython tests for src/bell_machine.py.

Run on the desktop / Pi:

    cd ~/Code/Sandbells/sandswing
    PYTHONPATH=src python3 tests/test_machine.py

No Pico, no CIRCUITPY, no Thonny. Feed fake (now_ms, wheel, stands)
timelines that look like a session: boot with a bell already up,
first wheel edge, direction lock, stand, lost-sight timeout.

If a rule change surprises you, add a timeline here *before* flashing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bell_machine import (
    BellMachine,
    IDLE,
    STOOD_HAND,
    STOOD_BACK,
    STOOD,
    MOVING,
    MOVING_CW,
    FAULT,
)


def test_boot_idle():
    m = BellMachine("1", t_ms=2000)
    assert m.boot(0, False, False, False) == IDLE


def test_boot_stood_hand():
    m = BellMachine("1", t_ms=2000)
    assert m.boot(0, False, True, False) == STOOD_HAND


def test_first_edge_starts():
    m = BellMachine("3", t_ms=2000)
    m.boot(0, False, False, False)
    st, ev = m.update(100, True, False, False)
    assert st == MOVING
    assert "started" in ev


def test_direction_lock():
    m = BellMachine("3", t_ms=2000)
    m.boot(0, False, False, False)
    m.update(100, True, False, False)
    st, ev = m.update(180, True, False, False, pattern="cw")
    assert st == MOVING_CW
    assert "direction" in ev


def test_stand_wins_when_quiet():
    m = BellMachine("2", t_ms=2000)
    m.boot(0, False, False, False)
    m.update(100, True, False, False)
    st, ev = m.update(400, False, False, True)
    assert st == STOOD_BACK
    assert "stood" in ev


def test_lost_sight():
    m = BellMachine("4", t_ms=1000, lost_mult=1.5)
    m.boot(0, False, False, False)
    m.update(10, True, False, False)
    st, ev = m.update(10 + 1600, True, False, False)
    assert st == FAULT
    assert "lost" in ev


def test_too_fast_is_fault():
    m = BellMachine("1", t_ms=2000, min_period_frac=0.15)
    m.boot(0, False, False, False)
    m.update(100, True, False, False)
    m.update(150, False, False, False)
    st, ev = m.update(180, True, False, False)
    assert st == FAULT
    assert "fault" in ev


if __name__ == "__main__":
    tests = [
        test_boot_idle,
        test_boot_stood_hand,
        test_first_edge_starts,
        test_direction_lock,
        test_stand_wins_when_quiet,
        test_lost_sight,
        test_too_fast_is_fault,
    ]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests), "passed")