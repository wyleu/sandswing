# RETIRE.md: repo `src/` (branch `mux_blend`) against `sandswing_next/src`

Nothing in the repo clone was deleted. "Imported by" is from `rg` over `src/*.py` and `src/tests/`.

| Repo file | Verdict | Reason | Imported by (repo) |
|---|---|---|---|
| `concentrator.py` | **KEEP** (edited) | The root. Builds and wires everything, then runs `ring.Ring`. It no longer execs a test program. | none (launched by `code.py`) |
| `ring.py` | **KEEP** (rewritten) | Now a small `Ring` class that concentrator hands objects to. No pin claims, no `fitted`, no own probe. MIDI is kept. | none |
| `tower_watch.py` | **KEEP** (unchanged) | Tower active, ringing-down or inactive; day tick. Paints a `panel.StatusSlot`, not the strip. | concentrator |
| `config_loader.py` | **KEEP** (unchanged) | `load_config()`. | concentrator, sandswing, board_pico, bell_detect, toll_detect, laser_sense |
| `farm_ws.py` | **KEEP** (unchanged) | HTTP `/status` + `/ws`. Only concentrator.py imports it now. | concentrator, sandswing, laser_sense |
| `farm_log.py` | **KEEP** (unchanged) | Size-capped log. | concentrator, sandswing, bell_detect, toll_detect |
| `sand_status.py` | **KEEP** (unchanged) | status-v1 builder, now called via `report.py`. | concentrator, sandswing, bell_detect |
| `code.py` | **KEEP** (unchanged) | Launcher (`startup.program`). | none |
| `boot.py` | **KEEP** (unchanged) | USB CDC + MIDI enable. | none |
| `bell_machine.py` | **KEEP** (unchanged, not wired yet) | Pure hand/back stroke state machine with tests. It is needed for Stage E (toll count, hand/back). | toll_detect, bell_detect, tests/test_machine.py |
| `tests/test_machine.py`, `tests/test_strip.py`, `tests/fake_strip.py` | **KEEP** | CPython tests for bell_machine and the tape model. Copied to `sandswing_next/tests/` (one path line changed). | none |
| `pins_from_settings.py` | **KEEP** (trimmed) | Only `load_pinmap` is left. It reads and claims nothing. `claim_lineup_hardware`, `claim_muxes` and `claim_threshold` are gone (the claims now live in tower_build and concentrator). `fitted`, `adc_laser` and `adc_pt` are dropped. | concentrator, ring, sandswing, board_pico, bell_detect, toll_detect |
| `mux4051.py` | **REPLACED-BY** `mux_select.py` + `sense_path.py` + `drive_path.py` | Each instance claimed A/B/C and its common: the "GP18 in use" bug. | pins_from_settings, bell, bell_detect |
| `head_probe.py` | **REPLACED-BY** `socket_survey.py` (+ `channel_gate.py`) | Boot-time laser-step probe with its own `_claim_addr`, 1 kHz threshold and a GP28 fallback. | concentrator (old) |
| `head_present.py` | **REPLACED-BY** `laser_measure.py` | Same "pulse the laser, see the port step" test. Its pin notes (A GP19, B GP18, C GP17) are stale. | none |
| `bell.py` | **REPLACED-BY** `bell_channel.py` + `tower_build.py` | `Farm`/`Bell` with a per-bell Mux4051 and `fitted`. | bell_detect, sandswing |
| `board_pico.py` | **REPLACED-BY** `loopback_test.py` | Bench loopback, mux walk and threshold ramp. These are now `dac_linearity` and `threshold_loopback`. Its header still says threshold PWM defaults to GP28. | none |
| `laser_sense.py` | **RETIRE** | Bench mapping of laser to mux port, plus a duty/current curve. Presence moves to `laser_measure.py`. The knee curve comes back as the reflector-stage routine (spec R4, not written yet). | none |
| `sandswing.py` | **RETIRE** | Old optical-lineup sweep, run as the "test program" on a tower mark. Survey + ring replace the boot path. The threshold step comes back as the reflector stage (spec §5). | none (exec'd by the old concentrator) |
| `toll_detect.py` | **RETIRE** (port first) | Funeral stroke counter on `pins.fitted` with its own claims. `Ring.strokes` counts trips now. Hand/back via `bell_machine` is the follow-up for Stage E. | none |
| `bell_detect.py` | **RETIRE** | Never launched ("do not point at this until..."). Long-term skeleton built on bell.py/mux4051. | none |
| `neopixel_test.py` | **RETIRE** | REPL strip chase (8 px). The panel is now a fixed 6 via `panel.py`. | none |
| `lib/bell_8_menu_LED_01.py`, `lib/i2c_scan.py`, `lib/i2cencoderlibv21*.py` | **RETIRE** (not this board) | I2C encoder/menu experiments (Dec 2025). Nothing in `src/` imports them. | none |
| `lib/adafruit_*` | out of scope | Libraries; deploy as needed (`farm_ws` needs `adafruit_httpserver`/`connection_manager`, `ring` needs `adafruit_midi`). | |
