# IR Bell Detector (CircuitPython)

Async 8-channel IR beam-break / latch detector for Raspberry Pi Pico (and compatible boards).  
Designed for electronic bells, pads, or similar triggers. Supports clean single-hit and multi-hit (middle-pulse) detection, USB MIDI output, file logging, and status LEDs.

## Features

- 8 independent channels with latching + middle-pulse validation
- Configurable timing (latch timeout, middle-gap window, scan rate)
- USB MIDI Note On/Off with velocity derived from hit duration
- Optional file logging
- Inverted LED feedback (LEDs off while latched)
- Status LED heartbeat
- All pins, timing, and output options live in `settings.json`
- Clean separation: loader (`code.py`) + reusable `hardware_config.py` + application program

## Hardware

### Default pin allocation (fully configurable in `settings.json`)

| Function          | GPIO   | Notes                          |
|-------------------|--------|--------------------------------|
| LED outputs       | GP0–GP7 | Inverted logic (LOW = latched) |
| IR emitter        | GP13   | 38 kHz carrier, ~33 % duty     |
| TX indicator      | GP12   |                                |
| Status LED        | GP22   | ~1 Hz blink                    |
| IR receivers      | GP14–GP21 | Active-low                    |

### Typical wiring

- Each IR receiver → GPIO + 10 kΩ pull-up to 3.3 V (or use internal pull-ups if preferred)
- IR emitter driven by GP13 through a suitable current-limiting resistor / transistor
- Status LEDs on GP0–GP7 with series resistors (common anode or cathode depending on your inversion preference)

## Software Requirements

- CircuitPython 8.x or 9.x
- Libraries from the [CircuitPython Library Bundle](https://circuitpython.org/libraries):
  - `adafruit_midi`
  - `adafruit_ticks`

## Quick Start

1. Copy the following files to the root of the `CIRCUITPY` drive:
   - `code.py` (the loader)
   - `hardware_config.py`
   - `pico_circuitp_8bell_scan_rs_long_midi_latch.py` (or place it in a `programs/` folder)
   - `settings.example.json`

2. Rename / copy the example config:
   ```bash
   cp settings.example.json settings.json