# Stroke stamps on the USB MIDI stream (`src/midi_stamp.py`)

USB MIDI has no time.  When the Pico's Wi-Fi is no use its USB goes into
sandbells2, whose `manage.py ingest_midi` turns each NoteOn into a stroke.
Stamped on arrival there, a ring with ±3 ms gaps by the Pico's keypad stamps
showed 3170-3244 ms (±40 ms of host jitter; 2026-11-03).  So right after
each stroke's NoteOn the Pico sends that stroke's own keypad stamp.

## Order and cost

* `ring.Ring._send`: the NoteOn first (MIDI is the only essential output),
  then at once the stamp SysEx, then the console line.  A laser-head stroke
  (`Ring.edge`, stamped by its edge) and a trigger-plug press
  (`Concentrator._trigger_note` -> `Ring.note(ch, True, stroke_ms)`) both.
  NoteOffs get no stamp.  A double (inside `MIN_STROKE_GAP_MS`) sends
  neither note nor stamp.
* One preallocated 13-byte `bytearray` filled in place, one
  `port.write` on the same usb_midi port as the notes.  A write that raises
  is counted (`/status` ring.stamp.errors), never raised.

## Bytes (version 1)

    F0 7D 53 01 sh sl cc t4 t3 t2 t1 t0 F7

| byte | meaning |
|------|---------|
| `F0` | SysEx start |
| `7D` | manufacturer ID: non-commercial (MIDI 1.0), used by no product, so any synth (Zynthian) drops it |
| `53` | `'S'`, sandswing |
| `01` | layout version |
| `sh sl` | seq, 14 bits (high 7, low 7).  Stroke stamps count 1 .. 16383 then 1 again; 0 = no stroke yet this boot (clock messages only) |
| `cc` | bell channel 1-8 (the stroke body's `ch`; not the MIDI channel, which stays 1).  `00` = clock message |
| `t4..t0` | ms, 35 bits as 5 x 7, most significant first: `t4<<28 | t3<<21 | t2<<14 | t1<<7 | t0` |
| `F7` | SysEx end |

The ms are `sntp_pico.TickClock` (supervisor.ticks_ms extended past its
2**29 wrap): the stroke's keypad edge stamp, on the same scale as the
stroke's `raw_tick` and the double-stroke gap.  It is free running, not 0 at
boot (CircuitPython starts ticks_ms just under 2**29), so the host only uses
differences and its own offset.  35 bits last about a year of uptime.

Example: ch 3, seq 1, t = 536 810 912 (0x1FFF15A0):
`F0 7D 53 01 00 01 03 01 7F 7C 2B 20 F7`.

## Clock message (`cc` = 0)

`Ring.clock()`, called at the end of every `poll_sense` pass (after the
edges), sends `TickClock.now()` with seq = the last stroke stamp's seq when
no stamp (stroke or clock) has gone for `midi.clock_s` (default 10 s).  So
while bells ring there are none; when quiet one every 10 s keeps the host's
offset estimate fresh, lets it see a stroke stamp it missed (seq moved),
and the first one after a (re)start (seq 0) tells it the Pico restarted.

## Settings

`settings.json` `"midi"` (optional).  A deployed board can keep the flat
form; that still means USB only:

    "midi": { "stamp_sysex": true, "clock_s": 10 }

USB and 5-pin DIN can also be separate, with the same basic keys.  `channel`
is MIDI channel 1-16; `notes` is one note number per bell channel; 5-pin
also names the UART TX pin (`tx_pin`: GP number, 16 on Pico 2 W).

    "midi": {
      "usb": {
        "enabled": true,
        "channel": 1,
        "notes": [65, 64, 62, 60, 58, 57, 55, 53],
        "stamp_sysex": true,
        "clock_s": 10
      },
      "five_pin": {
        "enabled": false,
        "tx_pin": 16,
        "channel": 1,
        "notes": [65, 64, 62, 60, 58, 57, 55, 53],
        "stamp_sysex": true,
        "clock_s": 10
      }
    }

`stamp_sysex: false` sends no SysEx on that port; `clock_s: 0` stamps
strokes but sends no clock.  `five_pin.enabled` defaults to false so a
board whose LED strip is still on GP16 is not driven as UART.
(`output.midi` is the old programs' section, unrelated.)

## Host side

Sandbells `events/midiparse.py` / `midiingest.py` (docs/midi-ingest.md):
pairs each stamp with the NoteOn before it, keeps offset = min over a window
of (host arrival - Pico ms), stroke time = Pico ms + offset.
