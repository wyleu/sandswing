# tests for sand_event.py (no board needed)
from support import case, finish
import sand_event


class FakeClock:
    def stamp(self, raw_tick=None):
        t = 1000 if raw_tick is None else int(raw_tick)
        return {
            "tick_ms": t,
            "utc_ms": 1_700_000_000_000 + t,
            "err_ms": 2.5,
            "sync": "synced",
            "acc": "ms",
            "epoch": 1,
            "session": 0,
            "mode": "free",
        }


@case
def test_schema_and_body():
    doc = sand_event.make_event(
        "stroke", FakeClock(), {"ch": 8},
        src="sandswing", device_id="concentrator", boot_id="b1", seq=7,
    )
    assert doc["schema"] == sand_event.SCHEMA
    assert doc["type"] == "stroke"
    assert doc["src"] == "sandswing"
    assert doc["id"] == "concentrator"
    assert doc["boot_id"] == "b1"
    assert doc["seq"] == 7
    assert doc["body"]["ch"] == 8
    assert doc["utc_ms"] is not None
    assert doc["sync"] == "synced"


@case
def test_null_clock():
    doc = sand_event.make_event("tick", None, {"tick": 1})
    assert doc["sync"] == "unsynced"
    assert doc["utc_ms"] is None
    assert doc["body"]["tick"] == 1


@case
def test_emitter_seq():
    em = sand_event.Emitter(src="sandswing", device_id="c1", boot_id="x")
    a = em.make(FakeClock(), "stroke", {"ch": 1})
    b = em.make(FakeClock(), "stroke", {"ch": 2})
    assert a["seq"] == 1
    assert b["seq"] == 2
    assert a["boot_id"] == b["boot_id"] == "x"


@case
def test_body_default_empty():
    doc = sand_event.make_event("panel", FakeClock())
    assert doc["body"] == {}


@case
def test_raw_tick_passed():
    doc = sand_event.make_event("stroke", FakeClock(), {}, raw_tick=42)
    assert doc["tick_ms"] == 42


@case
def test_null_clock_keeps_tick():
    from sntp_pico import TickClock
    raw = [5000]
    em = sand_event.Emitter(src="sandswing", ticks=TickClock(lambda: raw[0]))
    a = em.make(None, "stroke", {"ch": 1})
    raw[0] = 5250
    b = em.make(None, "stroke", {"ch": 1})
    assert a["tick_ms"] == 5000 and b["tick_ms"] == 5250
    assert a["utc_ms"] is None and a["sync"] == "unsynced"
    assert a["mode"] is None


finish()
