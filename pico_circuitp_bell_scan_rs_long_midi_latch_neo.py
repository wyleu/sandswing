# pico_circuitp_bell_scan_rs_long_midi_latch_neo.py
#
# CircuitPython IR Bell Detector – Latch Mode with MIDI + Log + NeoPixel status
#
# Overview
# --------
# Scans a configurable number of active-low IR (or switch) inputs.
# Uses a latch + middle-gap detector so short multi-hits on the same
# channel are treated as one continuous event.
# On latch start/end it:
#   - prints to console
#   - optionally logs to a file
#   - optionally sends USB MIDI NoteOn / NoteOff (velocity derived from duration)
#   - drives a NeoPixel / WS281x chain:
#       pixel 0  = permanent “data alive” indicator (diode-dropped first LED)
#       pixels 1..N = one LED per bell, lit while that channel is latched
#
# The individual GPIO LEDs from hardware_config are no longer used for
# bell indication; this frees those pins.
#
# Configuration is read from settings.json (see comments at bottom).
#
# Filename: pico_circuitp_bell_scan_rs_long_midi_latch_neo.py

import asyncio
import time
import sys
import supervisor
from adafruit_ticks import ticks_diff, ticks_ms
import json
from hardware_config import load_config, create_hardware

print("running on", sys.implementation.name)

if sys.implementation.name == "circuitpython":
    import board
    import digitalio
    import pwmio
    import usb_midi
    import adafruit_midi
    from adafruit_midi.note_on import NoteOn
    from adafruit_midi.note_off import NoteOff
    try:
        import neopixel
    except ImportError:
        neopixel = None
        print("neopixel library not found – NeoPixel support disabled")

# ------------------- Hardware & Detection -------------------
print("IR Bell Detector v2 - Latch Mode + NeoPixel")

config = load_config()
hw = create_hardware(config)

inputs     = hw["inputs"]
leds       = hw["leds"]          # kept for compatibility, but not used for bells
ir_pwm     = hw["ir_pwm"]
tx_led     = hw["tx_led"]
status_led = hw["status_led"]
pins       = hw["pins"]
det        = hw["detection"]

MODE              = det.get("mode", "latch_detect_with_middle")
LATCH_TIMEOUT_MS  = det.get("latch_timeout_ms", 100)
MIN_MIDDLE_MS     = det.get("min_middle_ms", 10)
MAX_MIDDLE_MS     = det.get("max_middle_ms", 80)
SCAN_INTERVAL_MS  = det.get("scan_interval_ms", 10)
NUM_BELLS         = det.get("num_bells", 8)

if NUM_BELLS > len(inputs):
    print(f"Warning: num_bells={NUM_BELLS} > available inputs ({len(inputs)}). Clamping.")
    NUM_BELLS = len(inputs)

print(f"Mode: {MODE}")
print(f"Bells / channels: {NUM_BELLS}")
print(f"Latch timeout: {LATCH_TIMEOUT_MS} ms")
print(f"Middle off range: {MIN_MIDDLE_MS}-{MAX_MIDDLE_MS} ms")
print(f"Scan interval: {SCAN_INTERVAL_MS} ms")
print(f"Pins loaded → inputs base GP{pins['input_base']}, "
      f"outputs base GP{pins['output_base']}, "
      f"IR GP{pins['ir_led']}, TX GP{pins['tx_led']}, "
      f"Status GP{pins['status_led']}, NeoPixel GP{pins.get('neopixel', '?')}")

tx_led.value = True
status_led.value = True

# Turn off the old individual GPIO LEDs (we no longer use them)
for led in leds:
    led.value = True

# ------------------- Load full settings.json -------------------
try:
    with open("settings.json", "r") as f:
        config = json.load(f)
    print("Loaded: settings.json")
except Exception as e:
    print("Config load error:", e)
    config = {"output": {"enabled_formats": ["console"]}}

enabled_formats = config.get("output", {}).get("enabled_formats", ["console"])

# ------------------- Output devices -------------------
# MIDI
midi = None
midi_channel = 0
midi_note_base = 60
midi_velocity = 100

if "midi" in enabled_formats:
    midi_conf = config.get("output", {}).get("midi", {})
    if midi_conf.get("enabled", False):
        try:
            midi_channel   = midi_conf.get("channel", 0)
            midi_note_base = midi_conf.get("note_base", 60)
            midi_velocity  = midi_conf.get("velocity", 100)
            midi = adafruit_midi.MIDI(
                midi_out=usb_midi.ports[1],
                out_channel=midi_channel
            )
            print(f"USB MIDI initialized (ch {midi_channel+1}, base note {midi_note_base})")
        except Exception as e:
            print("MIDI init failed:", e)
            midi = None

# Logging
log_file = None
if "log" in enabled_formats:
    log_conf = config.get("output", {}).get("log", {})
    if log_conf.get("enabled", False):
        log_prefix = log_conf.get("filename_prefix", "bell_log_")
        try:
            filename = f"{log_prefix}session.txt"
            log_file = open(filename, "a")
            log_file.write(f"Log started (monotonic {time.monotonic():.1f})\n")
            log_file.flush()
            print(f"Logging to: {filename}")
        except OSError as e:
            print("Log open failed:", e)
            log_file = None

# NeoPixel
neo = None
status_color = (0, 32, 0)
bell_color   = (64, 0, 0)

if "neopixel" in enabled_formats and neopixel is not None:
    neo_conf = config.get("output", {}).get("neopixel", {})
    if neo_conf.get("enabled", False):
        try:
            neo_pin_num = pins.get("neopixel", neo_conf.get("pin", 15))
            neo_pin = getattr(board, f"GP{neo_pin_num}")

            brightness  = float(neo_conf.get("brightness", 0.25))
            order_str   = neo_conf.get("pixel_order", "GRB").upper()
            pixel_order = getattr(neopixel, order_str, neopixel.GRB)

            status_color = tuple(neo_conf.get("status_color", [0, 32, 0]))
            bell_color   = tuple(neo_conf.get("bell_color",   [64, 0, 0]))

            neo = neopixel.NeoPixel(
                neo_pin,
                NUM_BELLS + 1,
                brightness=brightness,
                auto_write=False,
                pixel_order=pixel_order
            )
            print(f"NeoPixel chain ready on GP{neo_pin_num} ({NUM_BELLS + 1} LEDs)")
        except Exception as e:
            print("NeoPixel init failed:", e)
            neo = None
    else:
        print("NeoPixel disabled in settings")
else:
    if neopixel is None:
        print("NeoPixel library missing")
    else:
        print("NeoPixel not in enabled_formats")

# ------------------- Handlers -------------------
def timestamp():
    return time.monotonic()

def duration_to_velocity(ms: int) -> int:
    MAX_VEL = 127
    MIN_VEL = 35
    SHORTEST = 30
    LONGEST = 450
    if ms <= SHORTEST:
        return MAX_VEL
    if ms >= LONGEST:
        return MIN_VEL
    ratio = (ms - SHORTEST) / (LONGEST - SHORTEST)
    return max(MIN_VEL, MAX_VEL - int(ratio * (MAX_VEL - MIN_VEL)))

class OutputHandler:
    def __init__(self, name, enabled):
        self.name = name
        self.enabled = enabled

    def on_start(self, bell_num, extra_info=None):
        pass

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        pass

    def on_startup(self):
        status = "enabled" if self.enabled else "disabled"
        print(f"  [{self.name:10}] {status}")

class ConsoleHandler(OutputHandler):
    def __init__(self):
        super().__init__("console", True)

    def on_startup(self):
        print(f"  [console   ] enabled (always on)")

    def on_start(self, bell_num, extra_info=None):
        print(f"{timestamp():.3f} Bell {bell_num} started (latched)")

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        msg = f"{timestamp():.3f} Bell {bell_num} DETECTED (count: {count})"
        if extra_info:
            msg += f" {extra_info}"
        if velocity is not None:
            msg += f" vel={velocity}"
        print(msg)

class LogHandler(OutputHandler):
    def __init__(self, log_file):
        super().__init__("log", log_file is not None)
        self.log_file = log_file

    def on_startup(self):
        if self.enabled:
            print(f"  [log       ] enabled → writing to session log")
        else:
            print(f"  [log       ] disabled")

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        if not self.enabled or not self.log_file:
            return
        msg = f"{time.monotonic():.3f} Bell {bell_num} DETECTED (count {count})"
        if extra_info:
            msg += f" - {extra_info}"
        if velocity is not None:
            msg += f" vel={velocity}"
        self.log_file.write(msg + "\n")
        self.log_file.flush()

class MIDIHandler(OutputHandler):
    def __init__(self, midi, channel, note_base, default_velocity=100):
        super().__init__("midi", midi is not None)
        self.midi = midi
        self.channel = channel
        self.note_base = note_base
        self.default_velocity = default_velocity

    def on_startup(self):
        if self.enabled:
            print(f"  [midi      ] enabled  ch {self.channel+1}, base note {self.note_base}")
        else:
            print(f"  [midi      ] disabled")

    def on_start(self, bell_num, extra_info=None):
        if not self.enabled:
            return
        note = self.note_base + (bell_num - 1)
        print(f"{timestamp():.3f} MIDI NoteOn ch{self.channel+1} note {note} vel {self.default_velocity}")
        self.midi.send(NoteOn(note, self.default_velocity))

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        if not self.enabled:
            return
        note = self.note_base + (bell_num - 1)
        use_vel = velocity if velocity is not None else self.default_velocity
        print(f"{timestamp():.3f} MIDI NoteOff ch{self.channel+1} note {note} (vel was {use_vel})")
        self.midi.send(NoteOff(note, 0))

class NeoPixelHandler(OutputHandler):
    def __init__(self, neo, status_color, bell_color):
        super().__init__("neopixel", neo is not None)
        self.neo = neo
        self.status_color = status_color
        self.bell_color = bell_color
        if self.enabled:
            self.neo[0] = self.status_color
            for i in range(1, len(self.neo)):
                self.neo[i] = (0, 0, 0)
            self.neo.show()

    def on_startup(self):
        if not self.enabled:
            print(f"  [neopixel  ] disabled")
            return

        print(f"  [neopixel  ] enabled – colour cycle for 10 s …")
        cycle = [
            (80, 0, 0), (0, 80, 0), (0, 0, 80),
            (80, 40, 0), (40, 0, 80), (0, 60, 60)
        ]
        for i in range(20):                     # 20 × 0.5 s = 10 s
            c = cycle[i % len(cycle)]
            for p in range(len(self.neo)):
                self.neo[p] = c
            self.neo.show()
            time.sleep(0.5)

        # Return to idle
        self.neo[0] = self.status_color
        for i in range(1, len(self.neo)):
            self.neo[i] = (0, 0, 0)
        self.neo.show()
        print(f"  [neopixel  ] colour cycle finished – ready")

    def on_start(self, bell_num, extra_info=None):
        if not self.enabled:
            return
        idx = bell_num
        if 1 <= idx < len(self.neo):
            self.neo[idx] = self.bell_color
            self.neo[0] = self.status_color
            self.neo.show()

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        if not self.enabled:
            return
        idx = bell_num
        if 1 <= idx < len(self.neo):
            self.neo[idx] = (0, 0, 0)
            self.neo[0] = self.status_color
            self.neo.show()

    def all_off(self):
        if self.enabled:
            for i in range(len(self.neo)):
                self.neo[i] = (0, 0, 0)
            self.neo.show()
            
    def pulse_status(self):
        """Brief flash of the status LED (pixel 0)."""
        if not self.enabled:
            return
        self.neo[0] = self.status_color
        self.neo.show()
        time.sleep(0.12)               # short pulse
        self.neo[0] = (0, 0, 0)
        self.neo.show()

# Build the handler list
handlers = [ConsoleHandler()]

if log_file is not None:
    handlers.append(LogHandler(log_file))

if midi is not None:
    handlers.append(MIDIHandler(midi, midi_channel, midi_note_base, midi_velocity))

neo_handler = None
if neo is not None:
    neo_handler = NeoPixelHandler(neo, status_color, bell_color)
    handlers.append(neo_handler)

print("\n=== Output Handlers ===")
for h in handlers:
    h.on_startup()
print("=======================\n")
print(f"Active handlers: {len(handlers)}")

# ------------------- State -------------------
last_states      = [False] * NUM_BELLS
last_raw         = [False] * NUM_BELLS
last_pulse_times = [0] * NUM_BELLS
off_starts       = [0] * NUM_BELLS
start_times      = [0] * NUM_BELLS
saw_middles      = [False] * NUM_BELLS
hit_counts       = [0] * NUM_BELLS

# ------------------- Coroutines -------------------
async def input_scanner():
    while True:
        now = supervisor.ticks_ms()
        for i in range(NUM_BELLS):
            raw = not inputs[i].value          # active low

            if raw:
                last_pulse_times[i] = now
                if off_starts[i] != 0:
                    off_length = ticks_diff(now, off_starts[i])
                    if last_states[i] and MIN_MIDDLE_MS <= off_length <= MAX_MIDDLE_MS:
                        saw_middles[i] = True
                off_starts[i] = 0

                # Rising edge → start of event
                if not last_states[i]:
                    last_states[i] = True
                    start_times[i] = now
                    bell_num = i + 1
                    for handler in handlers:
                        handler.on_start(bell_num)
            else:
                if last_raw[i]:
                    off_starts[i] = now

            # Latch timeout → end of event
            if last_states[i]:
                elapsed = ticks_diff(now, last_pulse_times[i])
                if elapsed > LATCH_TIMEOUT_MS:
                    overall_length = ticks_diff(now, start_times[i])
                    bell_num = i + 1
                    velocity = duration_to_velocity(overall_length)
                    extra = f"overall: {overall_length} ms"
                    if not saw_middles[i]:
                        extra = f"single, {extra}"

                    hit_counts[i] += 1
                    for handler in handlers:
                        handler.on_end(bell_num, hit_counts[i], extra, velocity=velocity)

                    last_states[i] = False
                    saw_middles[i] = False
                    off_starts[i] = 0

            last_raw[i] = raw

        await asyncio.sleep_ms(SCAN_INTERVAL_MS)

async def blink_status_led():
    while True:
        status_led.value = True
        await asyncio.sleep_ms(490)
        status_led.value = False
        await asyncio.sleep_ms(10)

async def pulse_status_led():
    """Pulse NeoPixel status LED (pixel 0) once per second."""
    while True:
        if neo_handler is not None:
            neo_handler.pulse_status()
        await asyncio.sleep(1.0)

async def main():
    print("Starting tasks...")
    scanner_task = asyncio.create_task(input_scanner())
    status_task  = asyncio.create_task(blink_status_led())
    pulse_task   = asyncio.create_task(pulse_status_led()) 
    await asyncio.gather(scanner_task, status_task, pulse_task)

# ------------------- Run -------------------
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopped by user")
finally:
    ir_pwm.duty_cycle = 0
    tx_led.value = False
    status_led.value = True
    for led in leds:
        led.value = True
    if neo_handler is not None:
        neo_handler.all_off()
    if log_file:
        log_file.close()
        print("Log closed")
    print("Cleanup complete")