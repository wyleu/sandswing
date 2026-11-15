# Status over the USB data port (serial_status.py)

No Wi-Fi needed.  The Pico's USB cable to the Sandbells Pi carries three
things: the REPL console (`/dev/ttyACM0`), the usb_cdc data port
(`/dev/ttyACM1`, enabled in `boot.py`) and USB MIDI (the strokes).  This
uses only the data port: every `serial_status.period_s` seconds one line of
JSON, `\n` terminated.  Sandbells' `manage.py ingest_serial` (unit
`sandbells-ingest-serial`) reads it for the sandswing status page.

The line is the slim UDP heartbeat (`link_health.udp_heartbeat_doc`:
`sand.panel/v1`, type `heartbeat`, same envelope and body fields) plus
`mem_free`, `gc_heap_kb`, `edges_lost`, `late_drains`, `strokes_undelivered`,
`watchdog`, `thr` (per-channel threshold `target` and last GP28 `readback`,
0-based, `null` if none), `"via": "usb_cdc"`, and the writer's own counters:

    "serial": {"period_s": 10, "lines": 41, "unread": 0, "dropped": 0,
               "cut": 0, "errors": 0, "last_error": null}

Its `seq` is the last heartbeat's (the line is not counted as a heartbeat).
Typical line about 600 B; the biggest possible about 1.7 kB.

## Never in the way

* Nothing is built while no host has the port open (`unread` counts those).
* Writes use `write_timeout = 0`: the USB buffer (a few hundred bytes) takes
  what it can and the rest goes 50 ms later.  A line still not out when the
  next one is due is given up (`cut`); the next starts on a fresh line.
* Its own asyncio task; `step()` never raises (`errors`, `last_error`).
* No `usb_cdc`, no data port, or `enabled: false`: nothing runs.

## Settings

`settings.json` (optional; these are the defaults):

    "serial_status": { "enabled": true, "period_s": 10, "leds": {"enabled": true} }

`period_s` is never below 2.

## LED echo (`serial_status.leds`)

The heartbeat's `panel` is a snapshot every 10 s.  With `leds.enabled` the
Pico also sends the front panel as it is painted, on each change:

    {"t":"led","n":12,"p":[[0,32,0],[48,48,48],...],"s":["ws_client","response",...]}

* `LedEcho` (serial_status.py) is the state model.  `concentrator._paint`
  (panel_task, every 50 ms) hands it the frame's arguments: a compare,
  nothing built.  The task looks every `SERIAL_LED_CHECK_S` (50 ms) while a
  host has the port open.
* At most one line per `SERIAL_LED_MIN_S` (0.25 s).  The first state after
  a send is the one sent next, so a stroke's white flash is held on the wire
  for at least 0.25 s rather than lost; flashes closer than that merge.
* The ON phase of the panel, as the heartbeat's: blinking is not echoed.
* A heartbeat line part-written: the LED line waits.  The USB buffer takes
  none of it: not started (`leds.dropped`), tried again 0.25 s later.
  No host: nothing built; a host opening the port gets the current state.
* About 180 B a line (under the 256 B USB buffer, one write).  Garbage per
  line is about one panel frame, and panel_task already builds one every
  50 ms; at most 4 lines a second.
* Counters in the heartbeat line: `"serial": {..., "leds": {"lines",
  "dropped", "min_s"}}` (`null` when off).
* Off: `"leds": {"enabled": false}`, or on the Sandbells side
  `sudo systemctl stop sandbells-ingest-serial` (no host: nothing is sent).

## Live tune (`tune.py`)

The same data port also accepts command lines (and writes a short ack).
Nothing is saved: `settings.json` is never written.  Limits and steps come
from `threshold` and `laser` in settings (defaults in `config_loader.DEFAULTS`).

    {"cmd":"tune","ch":1,"thr":32768}
    {"cmd":"tune","ch":1,"thr_step":1}
    {"cmd":"drive","ch":1,"drive":50000}
    {"cmd":"drive","ch":1,"drive_step":-1}
    {"cmd":"tune_get"}
    {"cmd":"tune_get","ch":1}

Acks: `{"t":"tune_ack","ok":true,"ch":1,"thr":...,"rb":...,"drv":...,"ma":...,"why":null}`.
`tune_get` replies with `"t":"tune"`.  Adjustments are refused while the
tower is not quiet or a survey is running.  A raise of laser duty is
refused if the measured current is at or above `laser.max_ma`.

Sandbells' `ingest_serial` currently opens the port read-only, so it cannot
send these commands yet.

## Sandheads and errors on this port

* `{"cmd":"align",…}` lines are the sandheads scope (`docs/align-scope.md`):
  the page's commands relayed by Sandbells' `ingest_serial`; frames / acks /
  info come back as `sand.panel/v1` lines of type `align`, `align_ack`,
  `align_info`.
* One line at a time: `SerialStatus.step(now, hold=…)` starts nothing while a
  tune ack / align frame is part-written, and `SerialTune` holds its line
  while a heartbeat / LED line is part-written.
* A heartbeat that cannot be built (e.g. `MemoryError`) is replaced by
  `{"t":"serr","n":<errors so far>,"e":"<repr, cut>"}`, and the console says
  so (the first time, then every `SERIAL_ERR_LOG_EVERY`).  Before this the
  error counters only travelled inside the heartbeat itself, so a heartbeat
  that never got built was invisible.
* With `link.ring_send` `"off"` the heartbeat's `net` is the brief
  (`HB_UDP_NET`), as in `"udp"`: the serial line keeps only those keys, so
  the 60-key `link_status()` copy every heartbeat was heap for nothing.
