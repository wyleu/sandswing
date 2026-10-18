# test_ring.py -- ring.Ring with plain fakes (no board modules).
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import ring

results = []


def case(fn):
    try:
        fn()
        results.append(True)
        print("  ok  ", fn.__name__)
    except Exception as e:
        results.append(False)
        print("  FAIL", fn.__name__, repr(e))


class Pin:
    def __init__(self, v=True):
        self.value = v


class Pwm:
    duty_cycle = 0


def rig(n=4):
    ins = [Pin(True) for _ in range(n)]
    las = [Pwm() for _ in range(n)]
    sent = []
    r = ring.Ring(ins, las, send_fn=lambda ch, note, on: sent.append((ch, note, on)))
    return r, ins, las, sent


@case
def test_lasers_on_for_heads_only_and_owns_no_pins():
    r, ins, las, _ = rig()
    r.start({0: "head_new", 1: "head_old", 2: "loopback", 3: "empty"})
    assert [l.duty_cycle for l in las] == [65535, 65535, 0, 0]
    assert sorted(r.heads) == [0, 1]
    src = open(ring.__file__).read()
    assert "import board" not in src and "load_pinmap" not in src and "fitted" not in src


@case
def test_new_head_trip_sends_note_and_counts_stroke():
    r, ins, las, sent = rig()
    r.start({0: "head_new"})              # rest high (not held)
    ins[0].value = False                  # beam broken -> held -> trip
    r.poll()
    assert sent == [(0, 65, True)] and r.tripped() == {0} and r.strokes[0] == 1
    ins[0].value = True
    r.poll()
    assert sent[-1] == (0, 65, False) and not r.tripped()


@case
def test_old_head_rest_low_trip_on_rise():
    r, ins, las, sent = rig()
    ins[1].value = False                  # old head: held at rest
    r.start({1: "head_old"})
    r.poll()
    assert sent == []                     # resting is not a note
    ins[1].value = True
    r.poll()
    assert sent == [(1, 64, True)]


@case
def test_stop_turns_notes_and_lasers_off_and_restart():
    r, ins, las, sent = rig()
    r.start({0: "head_new", 2: "head_new"})
    ins[2].value = False
    r.poll()
    r.stop()
    assert sent[-1] == (2, 62, False) and all(l.duty_cycle == 0 for l in las)
    r.poll()                              # stopped: nothing happens
    r.start({0: "head_new"}, duty={0: 40000})
    assert las[0].duty_cycle == 40000 and las[2].duty_cycle == 0
    assert r.status()["heads"] == {1: "head_new"}


@case
def test_open_midi_without_usb_midi_is_none():
    assert ring.open_midi() is None


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
sys.exit(1 if n_fail else 0)
