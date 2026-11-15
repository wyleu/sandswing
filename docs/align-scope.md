# sandheads scope (`src/align_scope.py`, `src/pt_level.py`)

A live view of ONE laser head's comparator output, its readings and live
nudges, for Sandbells' **sandheads** page (`/farm/heads/`, `align.js`).  The
page talks to the Pico directly over the `/ws` it already uses.

**Live only.**  Nudges go through `tune.Tuner`, the same RAM-only store, rules
and settings limits as the USB tune commands (`docs/serial-status.md`).
Nothing writes `settings.json`; a reboot returns to the saved values.  A drive
set here is kept across a re-survey until reboot (`Tuner.drive_map`).

**Over /ws, or over USB serial.**  With `link.ring_send` `"udp"` or `"off"`
there is no TCP server.  Then the same protocol runs over the USB data port
(`usb_cdc.data`, host `/dev/ttyACM1`): Sandbells' `ingest_serial` (the one
owner of that port) writes the page's commands as one JSON line each, and
the Pico's replies come back as lines among the heartbeat / LED lines.
`tune.SerialTune` hands any line containing `"align"` to
`Aligner.on_message("usb", text)` (`align_scope.SERIAL_CID`); `send_to("usb",
…)` goes to `SerialTune.send` only when there is no USB MIDI out; normally the
readings and acks for the USB host go out as **MIDI Control Change** (see
"MIDI CC map" below) and nothing comes back on the data port.  Reports to the USB host are slower:
`SERIAL_FRAME_MS` 500 ms (1000 ms while the tower is not quiet).  If the host
closes the port (DTR down) the scope stops at the next frame; if the host just
goes quiet, the `KEEPALIVE_MS` timeout (10 s) stops it.  Only one line is in
flight on the port at a time: SerialStatus starts no heartbeat / LED line
while a frame is part-written and the other way round, and a part-written
line is always finished before a newer one starts.

## Active head

Survey kind `head_new` or `head_old` (`tune.ACTIVE_KINDS`).  `head_old` has
no threshold wire: threshold nudges are refused.

## Client -> Pico (text frames on /ws)

| frame | meaning |
|---|---|
| `{"cmd":"align","info":true}` | kinds, active heads, steps, limits, PT wire |
| `{"cmd":"align","ch":N,"scope":true}` | start the scope on head N, or keep it alive (page: every 2 s) |
| `{"cmd":"align","ch":N,"scope":false}` | stop |
| `{"cmd":"align","ch":N,"thr":1}` / `-1` | threshold target one settings step (`threshold.step`) |
| `{"cmd":"align","ch":N,"drive":1}` / `-1` | laser duty one settings step (`laser.step`) |
| `{"cmd":"align","ch":N,"thr_mid":true}` | threshold target to the middle of `threshold.min_duty`..`max_duty` (32768 with the defaults; a new head is also seeded at `threshold.default_duty`, 32768) |

Channels are 1-based.  `farm_ws._receive` hands every text frame containing
`"align"` to `Aligner.on_message(client_id, text)`, which only queues it
(`MAX_PENDING`); `Concentrator.align_task` does the work.  The page's
`sand.hello/v1` heartbeat keeps the client live as for any client.

## Pico -> the asking client only

Replies are queued for that one client (`farm_ws.send_to`) and written by the
poll task inside its budget; never broadcast, never kept for replay, never
written from the sense path.  If that client is lagging or replaying, a frame
is dropped (`farm_ws.align_stats`).  No `seq` field (so farm_ws never takes a
frame for a stroke).

`type:"align"`, every `FRAME_MS` (200 ms; 400 ms while the tower is not quiet):

```json
{"schema":"sand.panel/v1","type":"align","src":"sandswing","id":"sandswing-pico",
 "boot_id":"1a2b3c4d","tick_ms":35150231,
 "body":{"ch":1,"n":101,"dt":20.0,"gap":20,"bits":"f81f…","ma":4.9,"rb":23500,
         "thr":23500,"drv":65535,"pt":1.562,"pt_span":0.004,"tower":"inactive","busy":false}}
```

| field | meaning |
|---|---|
| `n`, `bits` | the sense level, one sample per sense pass, oldest first, 1 = high, MSB-first hex; an edge to held since the last pass makes that sample 0, so a break shorter than a pass still shows |
| `dt`, `gap` | mean / longest ms between samples (a held loop shows in `gap`) |
| `ma` | laser current (Tuner._measure: GP26 through the mux, minus the survey's dark level), at most every `MEASURE_MS` |
| `rb` | GP28 threshold readback held by the refresher |
| `thr`, `drv` | threshold target duty, laser duty now |
| `pt`, `pt_span` | raw PT level on the Orange/White wire, volts (mean, max-min over `PT_N` reads); `null` on every channel but the wired one |
| `busy` | true: `ma` is an older reading (tower not quiet, survey running, mux held).  `pt` is read every frame even then: GP27 alone, no mux, `PT_N` conversions, well under 1 ms |

`type:"align_ack"` after each command (not a keep-alive):
`{"what":"thr"|"drive"|"scope"|"bad","ch":N,"ok":bool,"why":…,"thr":…,"drv":…,"tower":…}`.
`why` is the Tuner's refusal (`not an active head (empty)`, `tower not quiet
(active): strokes in progress`, `survey running`, `old head has no
threshold`, `too fast`, `at limit`, `laser is off`, `laser current over 60 mA`,
`busy`) or the parser's.  Unsolicited `{"what":"scope","ok":false,"why":
"timeout"|"head gone"|"low memory"}` says the scope stopped itself.

`type:"align_info"`: `kinds`, `active`, `scope`, `tower`, `step`, `limits`,
`frame_ms`, `keepalive_ms`, `pt` (`{"ch":1,"gp":27,…}` or null).

## PT level (GP27)

`pins.pt_level` = `{"gp": 27, "ch": 1}` by default (no settings change
needed); `false` turns it off.  Channel 1's Orange/White goes straight to
GP27 on the concentrator (no mux, no filter).  Heads without that wire
(heads 3 and 4 for now) leave GP27 floating; the firmware cannot tell (a pull
test is unsafe on the RP2350, erratum E9), so the page labels the reading as
real only on a head with the wire, and `pt_span` shows how noisy it is.
`tune_get` acks carry `pt` / `pt_span` too.

## Reliability rules

* Idle: one `if` per sense pass, an `align_task` wake every `IDLE_S`.
* Per sample nothing is allocated (128-slot `bytearray` + `array('I')`).
  Per frame: one short hex string and the JSON text.
* The mux is only taken while the tower is quiet and no survey runs, at most
  every `MEASURE_MS` (one select, then a ~4 ms GP26 burst, as tune_get).

## Laser current

The laser current is chopped by its 1 kHz PWM, so a quick burst of reads
lands on a random part of on / off (at 25 % duty: mostly 0.2-1 mA, now and
then 17-22 mA).  `tune.Tuner._measure` discards the first read after the
select, then reads GP26 `TUNE_ADC_N` (131, prime) times spread evenly over
`TUNE_ADC_PERIODS` (4) whole PWM periods (period from the laser PWM's
frequency; at most `TUNE_BURST_MAX_MS`), paced by `time.monotonic_ns`
(1/32768 s steps on the RP2350).  The reported `ma` is the mean of the last
`TUNE_MA_BURSTS` bursts at that duty, above the survey's dark level: the
**average** current.  `ma_on` = `ma` / duty is the current while the laser
is on, comparable with the survey's full-duty reading.  The MIDI CC report
carries only `ma` (`CC_MA`); the host works out `ma_on` from `CC_MA` and
`CC_DRV` (a full report is 15 of `MAX_MSGS` 16 CCs).  The serial
heartbeat's `tune.burst_us` is the last burst's length.
* Every entry point catches everything; `MAX_ERRORS` in a boot switch the
  scope off for that boot.  Stops by itself: `KEEPALIVE_MS` without a
  request, head no longer active, the client gone, free heap under `MEM_MIN`.

## Not tested on hardware

Tested on the fakes only (`tests/test_align_scope.py`, `test_align_hw.py`,
`test_pt_level.py`, `test_farm_ws.py`).  Open: TCP load of 5 frames a second
on the real lwIP (ERR_MEM stalls are C code); real GP26 / GP27 values; what a
floating GP27 reads.

## PT level on the heartbeat (day / night drift)

The heartbeat body has `"pt": [ch, volts, spread]` read when the heartbeat is
built (`pt_level.brief`; `null` when not fitted or the wired channel is not an
active head).  It travels on the serial status line (`SERIAL_BODY_EXTRA`);
Sandbells' `ingest_serial` stores one reading every few minutes
(`HeadLevel`, see Sandbells `docs/sandheads-serial.md`).

## MIDI CC map (readings and acks to the USB host, `src/heads_cc.py`)

Commands go host -> Pico on the USB data port; readings and acks go Pico ->
host as Control Change on the USB MIDI out (one-way, as before), decoded by
Sandbells' `ingest_midi`.  All on **MIDI channel 16** (`CC_CHANNEL`; the
bells' NoteOns are on `midi.usb.channel`, 1).

| CC | bits | meaning |
|---|---|---|
| 102 `CC_HEAD` | 7 | head (channel) 1-16; **starts** a report |
| 103 `CC_FLAGS` | 7 | 1 beam clear now, 2 a low sample since the last report, 4 mA is an older reading, 8 tower quiet, 16 scope running on this head |
| 104 `CC_ACK_WHAT` | 7 | ack reports only: 1 thr, 2 drive, 3 scope, 4 bad, 5 thr_mid |
| 105 `CC_ACK_WHY` | 7 | ack reports only: 0 ok; 1 not an active head, 2 tower not quiet, 3 survey running, 4 old head has no threshold, 5 too fast, 6 at limit, 7 laser is off, 8 laser current over max_ma, 9 busy, 10 timeout, 11 head gone, 12 low memory, 13 bad command, 127 other |
| 20 / 52 `CC_THR` | 14 | threshold target duty, scaled 0..16382 (`duty = v * 65535 / 16382`) |
| 21 / 53 `CC_RB` | 14 | GP28 threshold readback, same scale |
| 22 / 54 `CC_DRV` | 14 | laser drive duty, same scale |
| 23 / 55 `CC_MA` | 14 | average laser current above dark, 0.01 mA (on-current = this / drive duty) |
| 24 / 56 `CC_PT` | 14 | PT level, mV |
| 25 / 57 `CC_PT_SPAN` | 14 | PT spread over the burst, mV |
| 106 `CC_SEQ` | 7 | report count mod 128; **ends** a report |

14-bit values are the standard MIDI pair: MSB on CC n (0-31), LSB on CC
n + 32; 16383 means "no value" (not measured, not wired).  Why 14 bits:
7-bit is 1/128 of full scale, i.e. 26 mV of PT level or about 1.3
threshold steps, too coarse to see day / night drift or one nudge; the
RP2350 ADC is 12-bit, so 14 bits lose nothing.

Frame reports: at most every 500 ms (`SERIAL_FRAME_MS`) while the tower is
quiet (15 messages: head, flags, 6 pairs, seq); every 1000 ms while strokes
are in progress, and then only head, flags, PT, spread and seq (7
messages).  Ack reports: one per command (not per keep-alive).

**Never in the way of a stroke:** reports are only sent while a host keeps
the scope alive (`KEEPALIVE_MS` 10 s; the page sends one every 2 s) and
stop at once when the host closes the data port; each report is built in a
preallocated buffer and written with ONE port write from `align_task`,
never from the sense path, so it cannot split or reorder a stroke's NoteOn
and stamp SysEx (asyncio is cooperative: a stroke found while a report is
being written goes out right after that one write, ~60 bytes on USB).
A report is skipped (counted) while `supervisor.runtime.usb_connected` is
False, and one the TX FIFO only partly takes is counted `short` (whole
messages only; the host drops a report without its SEQ).
Without a USB MIDI out the reports fall back to JSON lines on the data port.
