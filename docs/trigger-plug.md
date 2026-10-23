# Trigger plug (test switch)

A test plug for making a stroke at the concentrator without a bell.

| Pin (T568B colour) | Plug wiring |
|---|---|
| 2 Orange (threshold out, held) ↔ 3 Green/White (analog in, GP26 via inward 4051) | permanent strap, as in a loopback |
| 6 Green (laser out: GP8+n, 10 k pull-down at the pin, 33 Ω series to the socket) → 5 Blue/White (sense in, GP0+n, pulled up to 3.3 V) | normally-open push switch; no resistor needed (the 33 Ω is on the board) |
| 1, 4, 7, 8 | not connected |

## Survey signature (`socket_classify.py`)
The plug is classed `trigger` when all of these hold:
- the Orange→Green/White strap is seen (GP26/GP28 ≥ `LB_RATIO_MIN`);
- the Orange load is loopback-like (`l50` ≥ `TRIGGER_LOAD_MIN` 0.70, which is above the "10 k on Orange" band);
- there is no laser current;
- the sense does not follow the laser;
- Blue/White either idles high (switch open), or sits low whatever the laser does (a switch to 0 V held through the survey).

The check runs after the hard faults and "both straps = loopback", and before "partial loopback". The why text: "trigger plug (test switch) … press it for a test stroke".

Limits:
- A loopback plug whose 6→5 strap is open reads exactly the same, and is shown as `trigger`. Only a press tells them apart.
- A press during the survey: `SocketSurvey.classify` re-checks every `loopback` result `LOOPBACK_CONFIRM_N` (3) times, `LOOPBACK_CONFIRM_S` (0.2 s) apart. If the sense ever stops following, it was a press, and the plug is classed `trigger` (`m.follow_lost`). Only a switch held through the whole survey plus the re-checks (~1 s) reads as `loopback`, which is the same circuit. On release, the existing plug-change rule resurveys and gives `trigger`.
- Optional alternative: a switch from Blue/White to 0 V (pin 4). It also classifies as `trigger` when held through the survey ("switch held" why text).

## Behaviour
- Gate: `trigger` is not in `DRIVE_OK`, so the laser output stays at duty 0 (DC 0 V, never chopped). With the Green wiring, that low is what a press puts on Blue/White.
- Stroke = falling edge on Blue/White (active low, "held").
- Stroke (`Concentrator._trigger_edge`):
  - It fires on the first low sample (≤ one 20 ms loop).
  - The channel then disarms. It re-arms only after the switch has been seen open for `TRIGGER_REARM_MS` (50 ms), so press bounce and release bounce never fire.
  - `TRIGGER_MIN_GAP_MS` is 150 ms.
- Events: a normal `stroke` on the normal path (tower session, Sandbells, page 3), with body `{"ch": N, "test": true, "src": "trigger"}`.
- Survey: a press never resurveys. Held for `TRIGGER_STUCK_MS` (10 s) with the tower inactive, it asks for one resurvey (a stuck switch, or a head swapped in).
- Panel: bluish violet `(24, 0, 48)`, steady, at rest (distinct from loopback blue `(0, 0, 24)` and the LED6 watchdog magenta `(48, 0, 48)`); page 3's LED widget copies the RGB from the heartbeat, so it shows the same colour, white `response` while pressed.
- Self-test: a strap-B channel whose sense idles high and ignores the laser is noted as `trigger` (`ok: None`), not counted as a failure.
