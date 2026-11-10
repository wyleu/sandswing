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

    "serial_status": { "enabled": true, "period_s": 10 }

`period_s` is never below 2.
