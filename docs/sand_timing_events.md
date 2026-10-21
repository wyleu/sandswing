# Shared timing and events (Sand* devices)

Draft for wyleu, 2026-10-19.

Purpose: one small, generic layer that any Sand* Pico can use for a GPS timing
reference and for emitting timestamped events over WebSocket. First consumer is
**sandswing** (concentrator). The same modules should drop into **sandsense**
and later boards without rewriting the clock or the wire format.

This note sits beside `sandswing_commissioning_spec.md` section 7 (Time) and
the sandstore / sandbells architecture notes from today. It does not replace
those; it names the shared code and the event shape.

---

## 1. What is shared, what is not

| Shared (generic) | Device-specific |
|---|---|
| Millisecond GPS SNTP clock (`sntp_pico.py`) | Optical sense, mux, heads, escapement PLL |
| Event stamp fields from `SntpClock.stamp()` | Meaning of `type` / payload body |
| HTTP `/status` + WS `/ws` server (`farm_ws.py`) | Status document builder (`sand_status.from_*`) |
| Event envelope (`sand.event/v1`) | When to emit (stroke, tick, fault, …) |
| Settings keys under `time.*` and `network.*` | `escapement.*`, head cal, panel layout |

Sandbells remains an optional display. Sandstore (or sandbells Django for now)
is the always-on event sink. Devices do not speak SQL.

---

## 2. Time lock (generic)

### Module: `sntp_pico.py`

Already written for sandswing; treat it as **farm-common**:

- `TickClock` — extends `supervisor.ticks_ms` past wrap.
- `SntpClock` — burst SNTP to local GPS boxes **by IP** (`time.ntp_ips`).
- States: `unsynced` | `synced` | `stale` | `held`.
- `stamp()` → `tick_ms`, `utc_ms` (or null), `err_ms`, `sync`, `acc`, `epoch`, `session`, `mode`,
  `srv` (time server IP), `stratum` (its stratum); `srv`/`stratum` are null while unsynced.
- Server choice (`time_servers.py`, `time_select.py`): `time.ntp_ips` may list
  every site's GPS box (e.g. home `192.168.0.221`, tower sandmon `10.42.0.1`).
  Only those on the device's own subnet are tried (all of them if none is).
  Before each free-mode burst each candidate gets one short probe; the
  lowest valid stratum (1–15) wins, then the lowest RTT, and the whole burst
  goes to that one server.  A change of server clears the drift fit and RTT
  history and starts a new `epoch` (console: `<t> time server <ip> stratum <n>`).
  No reselection while ringing.  `disagree` (in `/status` sync) is set when
  two valid servers differ by more than 20 ms.  While unsynced the client
  retries every 15 s instead of `interval_s`.

Rules (from commissioning spec T1–T25, summarised):

1. Stamp on the Pico at the moment of detection, never on the Pi.
2. Never use `time.monotonic()` or whole-second `adafruit_ntp` for event times.
3. Island network only: no public NTP pool.
4. While a device is in a “session” that must not jump (sandswing: tower
   ringing), call `set_ringing(True)` so the issued mapping freezes.
5. If a session starts unsynced, `utc_ms` stays null for that session; `tick_ms`
   is always present so times can be rebuilt later.

### Settings (same names on every device)

```json
"time": {
  "ntp_ips": ["192.168.0.221", "10.42.0.1"],
  "interval_s": 300,
  "burst": 8,
  "timeout_s": 0.25,
  "stale_after": 3
}
```

Optional later: map sandsense’s `time_hosts` into `time.ntp_ips` (IPs only;
CircuitPython DNS cannot resolve `.local`).

### Status fields (every device)

When synced, status-v1 SHALL include `tick_ms`, `utc_ms`, `sync`, `acc`.
`ts` = `utc_ms // 1000` when synced, else `0` (schema compatibility).

Sandswing today still sends `ts: time.time()` — that is the gap to close.

---

## 3. WebSocket transport (generic)

### Module: `farm_ws.py`

Intended for every Sand* Pico that serves status and events.

**Today**

| | sandsense | sandswing (`farm_ws`) |
|---|---|---|
| `GET /status` | yes | yes |
| `GET /ws` accept | yes (`Websocket(request)`) | advertised, **not accepted** |
| Push events | tick payload | `send()` exists, client never set |

**Required fix (generic):** restore the `/ws` route as in sandsense:

```python
@server.route(path_ws, GET)
def ws_route(request):
    global _ws
    if _ws is not None:
        try:
            _ws.close()
        except Exception:
            pass
    _ws = Websocket(request)
    return _ws
```

One client at a time is enough for v1 (sandstore or sandbells browser).
A later multi-client fan-out can live on the Pi, not on the Pico.

API stays:

- `start_server(cfg, payload_fn)` — HTTP status + WS accept
- `poll()` — from the main loop
- `send(obj)` — JSON push to the current client
- `ws_connected()` — for link LED / status

Paths and port from settings: `network.http_port`, `network.status_path`,
`network.ws_path`. Enable with `output.ws.enabled` (or existing
`enabled_formats`).

---

## 4. Event envelope (generic)

Schema name: **`sand.event/v1`**.

Every pushed event is one JSON object:

```json
{
  "schema": "sand.event/v1",
  "type": "stroke",
  "src": "sandswing",
  "id": "concentrator",
  "boot_id": "...",
  "seq": 1042,
  "tick_ms": 123456789,
  "utc_ms": 1760899200123,
  "err_ms": 2.4,
  "sync": "synced",
  "acc": "ms",
  "epoch": 3,
  "session": 1,
  "mode": "ringing",
  "srv": "192.168.0.221",
  "stratum": 1,
  "body": { }
}
```

| Field | Who | Notes |
|---|---|---|
| `schema` | shared helper | Always `sand.event/v1` |
| `type` | device | Short verb: `stroke`, `tick`, `bell`, `panel`, `fault`, `head`, … |
| `src` / `id` | device settings | Family / instance |
| `boot_id` | device at boot | Ties `tick_ms` across reconnects |
| `seq` | shared helper | Monotonic per boot |
| stamp fields | `SntpClock.stamp()` | As T10 |
| `body` | device | Type-specific; consumers ignore unknown keys |

### Helper (proposed): `sand_event.py`

Tiny, no hardware imports:

```text
event(type, clock, body, *, src, id, boot_id, seq) -> dict
```

Merges `clock.stamp()` with the envelope. Devices call this, then
`farm_ws.send(...)`.

### Known types (v1)

| `type` | First used by | `body` (minimum) |
|---|---|---|
| `tick` | sandsense | `{ "tick": N, "virtual": bool, "vel": N }` |
| `bell` | sandswing (tape / jabber) | `{ "bell": N, "u": float, "amp": float }` — keep for kiosk |
| `stroke` | sandswing (counting) | `{ "ch": N, "bell": N or null, "edge": "rise"|"fall", ... }` |
| `panel` | sandswing | panel frame echo (existing L1 shape) |
| `fault` / `head` | sandswing | classification / plug changes |

Sandbells kiosk today understands `type: "bell"` (and bare `bell`). New
`stroke` events are for the store and for a single unified renderer later;
the kiosk may ignore them until wired.

Backward-friendly: sandsense can keep sending its current tick dict **and**
wrap it as `sand.event/v1` once the helper lands. Prefer one envelope.

---

## 5. Roles of the Pis (context)

- **Sandstore** (preferred long-term): always-on Pi; accepts WS (or devices
  push to it); appends events; optional query API for analysis.
- **Sandbells**: optional monitor; one renderer for live WS and DB replay;
  Django patterns stay separate from the event log.
- Until sandstore exists, sandbells Django may ingest the same envelope.

Devices open **one** WS toward the recording host (sandstore), or accept
one client (current Pico-as-server pattern). Recording must not depend on
the display being on.

---

## 6. Enablement order (sandswing first)

1. **Settings** — put GPS box IPs in `time.ntp_ips`. Confirm `SntpClock`
   reaches `synced` (status / boot record).
2. **`farm_ws.py`** — add the `/ws` accept route (copy sandsense). Verify
   `ws_connected()` and a browser or sandbells client stay up.
3. **`sand_event.py`** — add the envelope helper; unit-test on CPython.
4. **Status** — status-v1 uses `utc_ms` / `sync` from the clock (spec T13).
5. **Emit** — on each stroke/edge, `send(event("stroke", sntp, body, ...))`.
   Keep MIDI local. Keep `/status` for snapshots.
6. **LED 1** — link + time class per panel table (commissioning L3).
7. **Later** — sandsense: replace `adafruit_ntp` + monotonic `t` with
   `sntp_pico` + `sand.event/v1` `type: "tick"`. Same `farm_ws` / helper.

No SQL on the Pico. No device-specific imports inside `sntp_pico`,
`farm_ws`, or `sand_event`.

---

## 7. File ownership (proposed)

| File | Lives | Notes |
|---|---|---|
| `sntp_pico.py` | shared copy per repo, or a small `sand_common` tree | Identical on sandswing / sandsense |
| `farm_ws.py` | same | Accept route required |
| `sand_event.py` | same | New, tiny |
| `sand_status.py` | same idea, device `from_*` builders | Already shared pattern |
| `concentrator.py` / `sandsense.py` | device | Call stamp + send only |

Prefer **identical file text** across repos for the three shared modules
until a formal common package is worth the bother.

---

## 8. Acceptance checks (timing + events)

1. With GPS up: `sync == "synced"`, `acc == "ms"` within a few minutes of
   boot (after first good burst).
2. WS client receives a `sand.event/v1` object whose `utc_ms` is within
   `err_ms` of the Pi’s chrony time (spec T19a style).
3. Pull GPS: state becomes `stale`; events still carry growing `err_ms`;
   never labelled `synced`.
4. Sandswing: start a ring while synced → `mode: "ringing"`, `sync: "held"`;
   stroke `utc_ms` strictly non-decreasing.
5. Sandswing: start a ring while unsynced → `utc_ms` null for that session;
   `tick_ms` present.

---

## 9. Open decisions

1. Pico-as-server (clients connect in) vs Pico-as-client (connects out to
   sandstore). Today both sandsense and sandswing are server-shaped; sandstore
   may prefer devices connecting out so the store need not find them.
2. Single WS client on the Pico is enough for v1; confirm sandstore is that
   client, with sandbells reading from the store.
3. Whether `bell` (tape) and `stroke` (count) both ship in v1, or stroke only
   until the 4-1-2 strip is live.

---

## 10. Related docs

- `sandswing_commissioning_spec.md` — §7 Time, §11 Logging, panel L1–L3
- Sandbells `status-v1.md` — status document only (not the event stream)
- Sandsense README — working `/status` + `/ws` tick push (monotonic today)
