# test_sense_keys.py -- sense_keys.SenseKeys on the fake keypad (tests/fakes/keypad.py):
# settings defaults, polarity/pull, key -> channel, the event's own stamp,
# overflow logging, the pins <-> keypad hand-over.
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(HERE, "fakes"), os.path.join(HERE, "fakes", "stubs"),
                os.path.join(ROOT, "src")]
import _fake_hw
import keypad
import config_loader
import sense_keys
from sense_keys import SenseKeys

results = []
MASK = (1 << 29) - 1


def case(fn):
    _fake_hw.claimed.clear()
    _fake_hw.din_fn.clear()
    try:
        fn()
        results.append(True)
        print("  ok  ", fn.__name__)
    except Exception as e:
        import traceback
        traceback.print_exc()
        results.append(False)
        print("  FAIL", fn.__name__, repr(e))
    return fn


def rig(gps=(0, 1, 2, 3), qlen=sense_keys.EVENT_QUEUE_LEN):
    logs = []
    s = SenseKeys(gps, queue_len=qlen, log=logs.append)
    s.to_keys()
    s.keys.scanning = False          # inject() only
    return s, logs


@case
def test_settings_defaults_in_code_loader_and_example():
    assert (sense_keys.SCAN_INTERVAL_MS, sense_keys.EVENT_QUEUE_LEN) == (1, 64)
    assert sense_keys.settings({}) == (1, 64)
    assert sense_keys.settings({"inputs": None}) == (1, 64)
    d = config_loader.DEFAULTS["inputs"]
    assert d == {"scan_interval_ms": 1, "event_queue_len": 64}, d
    ex = json.load(open(os.path.join(ROOT, "settings_example.json")))["inputs"]
    assert ex == d, ex
    # a settings.json with no "inputs" section still gets the defaults
    cfg = config_loader.Config(config_loader._deep_merge(config_loader.DEFAULTS, {"pins": {}}))
    assert sense_keys.settings(cfg) == (1, 64)
    cfg = config_loader.Config(config_loader._deep_merge(
        config_loader.DEFAULTS, {"inputs": {"scan_interval_ms": 2}}))
    assert sense_keys.settings(cfg) == (2, 64)       # one key set, the other default
    assert sense_keys.settings({"inputs": {"scan_interval_ms": "x",
                                           "event_queue_len": 0}}) == (1, 1)


@case
def test_keys_polarity_pull_interval_queue_match_the_old_pin_code():
    s = SenseKeys([4, 5], scan_interval_ms=1, queue_len=64)
    assert s.mode == "pins"
    assert all(p.pull == "UP" for p in s._dio)      # old _claim_sense: Pull.UP
    s.to_keys()
    k = s.keys
    assert k.value_when_pressed is False and k.pull is True   # active low, pull-up
    assert k.interval == 0.001 and k.max_events == 64
    assert [p.gp for p in k.pins] == [4, 5]
    assert set(_fake_hw.claimed) == {4, 5} and set(_fake_hw.claimed.values()) == {"keypad.Keys"}


@case
def test_key_number_maps_to_channel_in_pin_order():
    s, _ = rig(gps=(7, 3, 5))
    s.keys.inject(2, True, 1000)                # key 2 = third sense GP (5) = ch index 2
    s.keys.inject(0, True, 1001)
    s.keys.inject(9, True, 1002)                # no such key: ignored
    assert list(s.drain()) == [(2, True, 1000), (0, True, 1001)]
    assert s.keys.pins[2].gp == 5 and s.keys.pins[0].gp == 7
    assert s.held == [True, False, True]


@case
def test_drain_keeps_each_events_own_timestamp_and_order():
    s, _ = rig()
    import supervisor
    saved = supervisor.ticks_ms
    supervisor.ticks_ms = lambda: 99999           # "now" when we drain: not used
    try:
        s.keys.inject(1, True, 500)
        s.keys.inject(1, False, 530)
        s.keys.inject(3, True, MASK - 2)          # stamped just before the wrap
        s.keys.inject(3, False, 4)                # and just after it (raw, wrapped)
        got = list(s.drain())
    finally:
        supervisor.ticks_ms = saved
    assert got == [(1, True, 500), (1, False, 530), (3, True, MASK - 2), (3, False, 4)], got
    assert s.drain() == ()


@case
def test_wraparound_through_tickclock_extend():
    from sntp_pico import TickClock, TICKS_PERIOD
    t = [TICKS_PERIOD - 100]
    clk = TickClock(lambda: t[0] & MASK)
    clk.now()
    pre, post = TICKS_PERIOD - 40, 25             # raw stamps either side of the wrap
    t[0] = TICKS_PERIOD + 300                      # drained well after the wrap ...
    assert clk.now() == TICKS_PERIOD + 300         # ... and the clock was read first
    a, b = clk.extend(pre & MASK), clk.extend(post)
    assert (a, b) == (TICKS_PERIOD - 40, TICKS_PERIOD + 25), (a, b)
    assert b - a == 65


@case
def test_first_scan_of_a_held_key_is_not_an_edge():
    _fake_hw.din_fn[1] = lambda: False             # ch2 held (active low) at hand-over
    s = SenseKeys([0, 1], log=lambda m: None)
    s.to_keys()
    assert s.held == [False, True]
    assert list(s.drain()) == []                   # keypad's first 'pressed' dropped
    _fake_hw.din_fn[1] = lambda: True              # released: a real edge
    ev = list(s.drain())
    assert [(c, h) for c, h, _ in ev] == [(1, False)], ev


@case
def test_overflow_is_logged_counted_and_cleared():
    s, logs = rig(qlen=4)
    for i in range(6):                             # 6 edges into a 4-deep queue
        s.keys.inject(0, i % 2 == 0, 100 + i)
    assert s.keys.events.overflowed
    got = list(s.drain())
    assert len(got) == 4 and s.overflows == 1, (got, s.overflows)
    assert not s.keys.events.overflowed            # cleared
    assert len(logs) == 1 and "EDGES LOST" in logs[0] and "overflow" in logs[0], logs
    assert s.status()["overflows"] == 1
    s.keys.inject(0, True, 200)
    s.drain()
    assert s.overflows == 1 and len(logs) == 1     # no new overflow, no new line


@case
def test_handover_keeps_queued_edges_and_reports_unseen_changes():
    s, _ = rig(gps=(0, 1))
    s.keys.inject(0, True, 700)                    # queued before a survey
    s.to_pins()
    assert 0 not in [p for p in _fake_hw.claimed if _fake_hw.claimed[p] == "keypad.Keys"]
    assert s.pins[0].value is True                 # pins mode: the live pin (idle high)
    s.pins[1].pull = "DOWN"                        # the survey switches pulls
    assert s._dio[1].pull == "DOWN"
    _fake_hw.din_fn[1] = lambda: False             # ch2 goes held during the survey
    s.to_keys()
    # the queued edge first, with its stamp; then what changed unwatched:
    # ch1 is clear again (no stamp), ch2 is held (no stamp)
    assert list(s.drain()) == [(0, True, 700), (0, False, None), (1, True, None)]
    try:
        s.pins[0].pull = "UP"
        raise AssertionError("pull set while keypad owns the pin")
    except RuntimeError:
        pass
    assert s.pins[1].value is False and s.handovers == 2
    s.deinit()
    assert not _fake_hw.claimed


def _other_gps(pins):
    """Every GP the settings give to something other than the sense inputs:
    lasers (PWM), NeoPixel, both 4051s' A/B/C/INH/COMMON, the threshold PWM
    (GP21) and its ADC read-back (GP28)."""
    out = set(int(g) for g in pins.get("laser_pwm") or [])
    if pins.get("neopixel") is not None:
        out.add(int(pins["neopixel"]))
    for sec in ("adc_mux", "threshold_mux"):
        m = pins.get(sec) or {}
        for k in ("a", "b", "c", "inh", "common", "pwm_gp", "sense_gp"):
            if m.get(k) is not None:
                out.add(int(m[k]))
    return out


@case
def test_keypad_pins_are_not_the_threshold_adc_or_pwm_pins():
    # keypad claims the sense GPs only; the threshold reload (PWM GP21 through
    # the output 4051, GP28 ADC) and laser-current ADC (GP26) never touch them
    for f in (os.path.join(ROOT, "settings_example.json"),
              os.path.join(HERE, "data", "settings_current.json")):
        pins = json.load(open(f))["pins"]
        sense = set(int(g) for g in pins["sense"])
        clash = sense & _other_gps(pins)
        assert not clash, (f, sorted(clash))


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
sys.exit(1 if n_fail else 0)
