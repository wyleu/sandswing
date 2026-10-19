# sand_event.py -- sand.event/v1 envelope for timed Sand* devices.
#
# Shared by sandswing, sandsense, and later boards. No hardware imports.
# Stamp fields come from SntpClock.stamp() (or any object with stamp()).
#
#   from sand_event import Emitter
#   ev = Emitter(src="sandswing", device_id="concentrator", boot_id=boot_id)
#   farm_ws.send(ev.make(sntp, "stroke", {"ch": 8, "bell": 8}))
#
SCHEMA = "sand.event/v1"

# Stamp keys produced by SntpClock.stamp() (spec T10 / T24).
_STAMP_KEYS = (
    "tick_ms", "utc_ms", "err_ms", "sync", "acc", "epoch", "session", "mode",
)


def _stamp_fields(clock, raw_tick=None, ticks=None):
    """Pull stamp dict from clock.  With no clock, tick_ms still comes from
    ticks (a TickClock) when given, so events stay in order; utc is null."""
    if clock is None:
        t = None
        if ticks is not None:
            t = ticks.now() if raw_tick is None else ticks.extend(raw_tick)
        return {
            "tick_ms": t,
            "utc_ms": None,
            "err_ms": None,
            "sync": "unsynced",
            "acc": None,
            "epoch": 0,
            "session": 0,
            "mode": None,
        }
    if raw_tick is None:
        st = clock.stamp()
    else:
        st = clock.stamp(raw_tick)
    out = {}
    for k in _STAMP_KEYS:
        out[k] = st.get(k)
    return out


def make_event(event_type, clock, body=None, *, src="", device_id="",
               boot_id="", seq=0, raw_tick=None, ticks=None):
    """One sand.event/v1 dict. body is copied into "body" (default {})."""
    doc = {
        "schema": SCHEMA,
        "type": str(event_type),
        "src": src or "",
        "id": device_id or "",
        "boot_id": boot_id or "",
        "seq": int(seq),
    }
    doc.update(_stamp_fields(clock, raw_tick, ticks))
    doc["body"] = {} if body is None else dict(body)
    return doc


class Emitter:
    """Per-boot sequence and identity for event() calls."""

    def __init__(self, src="", device_id="", boot_id="", ticks=None):
        self.ticks = ticks                       # TickClock fallback, or None
        self.src = src or ""
        self.device_id = device_id or ""
        self.boot_id = boot_id or ""
        self.seq = 0

    def make(self, clock, event_type, body=None, raw_tick=None):
        self.seq += 1
        return make_event(
            event_type,
            clock,
            body,
            src=self.src,
            device_id=self.device_id,
            boot_id=self.boot_id,
            seq=self.seq,
            raw_tick=raw_tick,
            ticks=self.ticks,
        )
