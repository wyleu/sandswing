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
`watchdog`, `"via": "usb_cdc"`, and the writer's own counters:

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
