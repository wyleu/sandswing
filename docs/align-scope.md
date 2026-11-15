# sandheads scope (`src/align_scope.py`, `src/pt_level.py`)

A live view of ONE laser head's comparator output, its readings and live
nudges, for Sandbells' **sandheads** page (`/farm/heads/`, `align.js`).  The
page talks to the Pico directly over the `/ws` it already uses.

**Live only.**  Nudges go through `tune.Tuner`, the same RAM-only store, rules
and settings limits as the USB tune commands (`docs/serial-status.md`).
Nothing writes `settings.json`; a reboot returns to the saved values.  A drive
set here is kept across a re-survey until reboot (`Tuner.drive_map`).

**Needs the /ws server.**  With `link.ring_send` `"udp"` or `"off"` there is
no TCP server, so the page cannot connect (it shows "retrying…").  The scope
then costs nothing: no hook is ever called.

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
| `busy` | true: `ma` / `pt` are older readings (tower not quiet, survey running, mux held) |

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
  every `MEASURE_MS` (one select + 32 ADC reads, as tune_get).
* Every entry point catches everything; `MAX_ERRORS` in a boot switch the
  scope off for that boot.  Stops by itself: `KEEPALIVE_MS` without a
  request, head no longer active, the client gone, free heap under `MEM_MIN`.

## Not tested on hardware

Tested on the fakes only (`tests/test_align_scope.py`, `test_align_hw.py`,
`test_pt_level.py`, `test_farm_ws.py`).  Open: TCP load of 5 frames a second
on the real lwIP (ERR_MEM stalls are C code); real GP26 / GP27 values; what a
floating GP27 reads.
