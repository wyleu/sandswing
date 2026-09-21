# sandswing

Church-bell **optical lineup / scan** on a Raspberry Pi Pico 2 W (CircuitPython).

GitHub: https://github.com/wyleu/sandswing  
Sister device: [sandsense](https://github.com/wyleu/sandsense) (tower clock escapement).

The README that shipped with this repo was a copy of the old 8-channel IR bell-detector text. That still describes the **legacy** `pico_circuitp_8bell_scan_*` programs in this tree. It does **not** describe `src/sandswing.py` (optical lineup).

## Two programs in one repo

| Program | Job | Trusted? |
|---|---|---|
| `src/sandswing.py` | Optical lineup: PWM lasers GP7–10, sense GP0–3, NeoPixel GP16, optional WS jabber | New (Sep 2026). Ran a sweep. Lasers on the bench still unproven. |
| `pico_circuitp_8bell_scan_*.py` | 8-channel IR latch + MIDI (the thing that detected bells for months) | Older. Still in git. Uses `hardware_config.py` + `input_base` / `output_base`. |
| root `sandswing.py` | Snapshot of the 20 Sep 08:46 lineup (`create_hardware`) | Pre-tidy copy |

`settings.json` → `startup.program` chooses which file `code.py` starts. Use `sandswing.py` for lineup, a `pico_circuitp_8bell_scan_*.py` name for the old detector.

## What `src/sandswing.py` does

- Pins from `settings.json` → `pins` via `pins_from_settings.py` (no `input_base + i`).
- Sweeps each laser 0–100% duty, reads matching photodiode (active-low).
- NeoPixel on GP16 (status + one pixel per channel).
- Optional fake rounds on WebSocket if `streams.bell.test_sweep` is on.
- Wi-Fi via `farm_ws` when configured.

## What it does not do

- Does not detect real bell blows (that is the old scan programs).
- Does not run the escapement ADS1115 path (that is sandsense).
- Does not own CIRCUITPY as git.

## Hardware (lineup loom, Sep 2026)

| Function | GP |
|---|---|
| Sense | 0, 1, 2, 3 (heads on 0 and 1) |
| Laser PWM | 7, 8, 9, 10 |
| NeoPixel | 16 |
| ADC laser I (optional) | 26, 27 |
| ADC PT (optional) | 28 |

If wiring is swapped, put the **observed** GPs in `settings.json` `"pins"."laser_pwm"` / `"sense"`.

## Shared farm pieces

Same launcher idea as sandsense: `code.py` + `config_loader.py` + `settings.json`.

Status reporting is **not** unified yet. Sandsense emits `sand.status/v1` via `sand_status.py`. Sandswing still uses `farm_ws` payloads. Next unification step: sandswing `/status` uses the same `sand.status/v1` envelope (`from_sandswing(...)` in a shared `sand_status.py`).

## How you develop (one way: host → Pico)

```text
sandswing/
  src/          # only files deployed
    boot.py
    code.py
    sandswing.py
    pins_from_settings.py
    config_loader.py
    farm_log.py
    farm_ws.py
    settings.json
    lib/
  tools/deploy.sh    # mpremote serial — not the CIRCUITPY mount
  firmware/          # Pico 2 W 10.0.3 UF2 + flash_nuke (not in git)
```

```bash
./tools/deploy.sh
```

CIRCUITPY is a stamp. Edit `src/` on the Pi. GitHub is the backup. Format with UF2 only when the volume is trash (`?` names, read-only). Then deploy again over serial.

## CircuitPython

Pico 2 W **10.0.3**. `lib/` is a 10.x bundle (`neopixel` 6.3.x loaded successfully on that firmware).
