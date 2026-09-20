# pico_circuitp_8bell_scan_rs_long_midi_latch.py

import asyncio
import time
import sys
import supervisor
from adafruit_ticks import ticks_diff, ticks_ms
import json
import os


print("running on", sys.implementation.name)

if sys.implementation.name == "circuitpython":
    import board
    import digitalio
    import pwmio
    import usb_midi
    import adafruit_midi
    from adafruit_midi.note_on import NoteOn
    from adafruit_midi.note_off import NoteOff
    
    from adafruit_datetime import datetime

# ------------------- Config -------------------
MODE = "latch_detect_with_middle"
print('IR Bell Detector v2 - Latch Mode')
LATCH_TIMEOUT_MS = 300     # > max middle off; adjust based on logs  was 100
MIN_MIDDLE_MS = 10         # min off for middle strip (ignore noise)
MAX_MIDDLE_MS = 80         # max off for middle strip
SCAN_INTERVAL_MS = 10
INPUT_BASE = 14            # GP14–GP21
OUTPUT_BASE = 0            # GP0–GP7
IR_LED_PIN = 13
TX_LED_PIN = 12
STATUS_LED_PIN = 22
DUTY_33 = 21845
CARRIER_FREQ = 38000

# ------------------- Pin Setup -------------------
inputs = []
for i in range(8):
    pin = digitalio.DigitalInOut(getattr(board, f"GP{INPUT_BASE + i}"))
    pin.direction = digitalio.Direction.INPUT
    inputs.append(pin)

leds = []
for i in range(8):
    pin = digitalio.DigitalInOut(getattr(board, f"GP{OUTPUT_BASE + i}"))
    pin.direction = digitalio.Direction.OUTPUT
    leds.append(pin)

tx_led = digitalio.DigitalInOut(board.GP12)
tx_led.direction = digitalio.Direction.OUTPUT

status_led = digitalio.DigitalInOut(board.GP22)
status_led.direction = digitalio.Direction.OUTPUT

ir_pwm = pwmio.PWMOut(board.GP13, frequency=CARRIER_FREQ, duty_cycle=DUTY_33)

tx_led.value = 1
status_led.value = 1
print("Async IR Bell Detector Ready (Latch Mode)")
print(f"Carrier: {CARRIER_FREQ} Hz @ ~33%")
print(f"Latch timeout: {LATCH_TIMEOUT_MS} ms")
print(f"Middle off range: {MIN_MIDDLE_MS}-{MAX_MIDDLE_MS} ms")
print(f"LED outputs: INVERTED (OFF when latched, ON when idle)")
print(f"Status LED: GP{STATUS_LED_PIN} blinking ~1 Hz")
print("Watch logs for timestamps, pulse lengths, and off periods to tune timeouts")

# ------------------- Load Config -------------------
CONFIG_FILE = "settings.json"
config = {}
try:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    print("Loaded:", CONFIG_FILE)
except Exception as e:
    print("Config load error:", e)
    config = {"wifi": {"ssid": ""}, "output": {"enabled_formats": [], "midi": {}, "log": {}}, "startup": {"program": 0}}

wifi_ssid = config["wifi"]["ssid"]
enabled_formats = config["output"]["enabled_formats"]

# ------------------- Output Setup -------------------
midi = None
midi_channel = 0
midi_note_base = 60
midi_velocity = 100

def timestamp():
    return time.monotonic()


def duration_to_velocity(ms: int) -> int:
    """Shorter = louder/harder → higher velocity"""
    if ms <= 40:
        return 127
    elif ms <= 80:
        return 120 - (ms - 40) // 2          # 120 → ~110
    elif ms <= 200:
        return 110 - (ms - 80) // 3          # 110 → ~77
    elif ms <= 400:
        return 80 - (ms - 200) // 5          # 80 → ~52
    else:
        return max(30, 60 - (ms - 400) // 10)  # floor around 30–40
    
def duration_to_velocity(ms: int) -> int:
    MAX_VEL = 127
    MIN_VEL = 35
    SHORTEST = 30   # ms → full velocity
    LONGEST  = 450  # ms → minimum velocity
    
    if ms <= SHORTEST:
        return MAX_VEL
    if ms >= LONGEST:
        return MIN_VEL
        
    # linear interpolation
    ratio = (ms - SHORTEST) / (LONGEST - SHORTEST)
    vel = MAX_VEL - int(ratio * (MAX_VEL - MIN_VEL))
    return max(MIN_VEL, vel)

if "midi" in enabled_formats:
    midi_conf = config["output"].get("midi", {})
    if midi_conf.get("enabled", False):
        try:
            midi_channel = midi_conf.get("channel", 0)
            midi_note_base = midi_conf.get("note_base", 60)
            midi_velocity = midi_conf.get("velocity", 100)
            midi = adafruit_midi.MIDI(midi_out=usb_midi.ports[1], out_channel=midi_channel)
            print(f"USB MIDI initialized (ch {midi_channel+1}, base note {midi_note_base})")
        except Exception as e:
            print("MIDI init failed (skipping MIDI output):", e)
            midi = None

log_file = None
if "log" in enabled_formats:
    log_conf = config["output"].get("log", {})
    if log_conf.get("enabled", False):
        log_prefix = log_conf.get("filename_prefix", "bell_log_")
        
        now_tuple = time.localtime()   # Wait — this doesn't exist either!
        # So instead, if you don't have RTC → use monotonic or skip fancy name
        # Simplest: use a counter or just fixed name during dev
        # filename = f"{log_prefix}latest.txt"   # append forever
        
        # If you have RTC and time.time() works:
        epoch = int(time.time())
        if epoch > 1000000000:  # rough check it's reasonable
            filename = f"{log_prefix}{epoch}.txt"
        else:
            filename = f"{log_prefix}session.txt"
        
        try:
            log_file = open(filename, "a")
            # Manual header without strftime
            if epoch > 1000000000:
                # crude approximation
                log_file.write(f"Log started approx {epoch} (epoch seconds)\n")
            else:
                log_file.write("Log started (no RTC set)\n")
            log_file.flush()
            print(f"Logging to: {filename}")
        except OSError as e:
            print("Log open failed:", e)
            log_file = None

# ------------------- Handlers -------------------
class OutputHandler:
    def __init__(self, name, enabled):
        self.name = name
        self.enabled = enabled

    def on_start(self, bell_num, extra_info=None):
        pass

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        pass

class ConsoleHandler(OutputHandler):
    def __init__(self):
        super().__init__("console", True)

    def on_start(self, bell_num, extra_info=None):
        pass
        #print(f"{timestamp()} Bell {bell_num} started (latched)")

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        print(f"{timestamp()} Bell {bell_num} DETECTED (count: {count})")
        if extra_info:
            print(f"{extra_info}")
        # You can now also use velocity if you want, e.g.:
        # if velocity is not None:
        #     print(f"  velocity: {velocity}")

class LogHandler(OutputHandler):
    def __init__(self, log_file):
        super().__init__("log", log_file is not None)
        self.log_file = log_file

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        if not self.enabled or not self.log_file:
            return
        now_str = f"{time.localtime()[3]:02d}:{time.localtime()[4]:02d}:{time.localtime()[5]:02d}"
        msg = f"{now_str} Bell {bell_num} DETECTED (count {count})"
        if extra_info:
            msg += f" - {extra_info}"
        if velocity is not None:
            msg += f" vel:{velocity}"
        self.log_file.write(msg + "\n")
        self.log_file.flush()

# Remove NoteOn from on_start
# Move sending to on_end only

class MIDIHandler(OutputHandler):
    def __init__(self, midi, channel, note_base, config_velocity=100):
        super().__init__("midi", midi is not None)
        self.midi             = midi
        self.channel          = channel
        self.note_base        = note_base
        self.config_velocity  = config_velocity   # renamed for clarity
        
    def on_start(self, bell_num, extra_info=None, velocity=None):
        # Optional: could send very short NoteOn with fixed vel for visual feedback
        # but usually better to wait
        pass

    def on_end(self, bell_num, count, extra_info=None, velocity=None):
        if not self.enabled:
            return
        note = self.note_base + (bell_num - 1)
        use_vel = velocity if velocity is not None else self.config_velocity
        # print(f"{timestamp()} MIDI NoteOn → NoteOff  ch{self.channel+1} note {note} vel {use_vel}")
        self.midi.send(NoteOn(note, use_vel))
        self.midi.send(NoteOff(note, 0))

handlers = [ConsoleHandler()]
if log_file is not None:
    handlers.append(LogHandler(log_file))
if midi is not None:
    handlers.append(MIDIHandler(midi, midi_channel, midi_note_base, midi_velocity))
    print("MIDI handler added")

print(f"Active handlers: {len(handlers)}")

# ------------------- State -------------------
last_states = [False] * 8          # latched / in on-period
last_raw = [False] * 8
last_pulse_times = [0] * 8
off_starts = [0] * 8
start_times = [0] * 8
saw_middles = [False] * 8
hit_counts = [0] * 8

# ------------------- Coroutines -------------------
async def input_scanner():
    """
    Background task that continuously scans 8 digital inputs (likely bells, pads, or switches)
    and implements a latching / edge-detection pattern with middle-pulse validation.

    Typical use-case: electronic drum/bell/trigger systems where:
    - A hit produces a low-going pulse (active low)
    - We want to detect clean "hits" while rejecting bounce / noise
    - We distinguish single vs. multiple hits (via middle pulse detection)
    """
    while True:
        # ────────────────────────────────────────────────
        # Get current time once per scan loop (monotonic ms)
        # Used for all timing comparisons in this iteration
        # ────────────────────────────────────────────────
        now = supervisor.ticks_ms()

        # Process each of the 8 input channels independently
        for i in range(8):
            # ────────────────────────────────────────────────
            # Read raw input state (active low assumed)
            #   inputs[i].value == True  → not pressed / idle (high)
            #   inputs[i].value == False → pressed       (low)
            # We invert it so raw == True means "active / pressed"
            # ────────────────────────────────────────────────
            raw = not inputs[i].value

            # ────────────────────────────────────────────────
            # CASE 1: Input is currently ACTIVE (pressed / low)
            # ────────────────────────────────────────────────
            if raw:
                # Update the last time we saw any activity on this channel
                # (used later for latch timeout detection)
                last_pulse_times[i] = now

                # If we were previously in an OFF period (between pulses),
                # calculate how long that OFF gap lasted
                if off_starts[i] != 0:
                    off_length = ticks_diff(now, off_starts[i])

                    # Check if this OFF period looks like a valid "middle" separation
                    # between two hits → typical of a double-hit / buzz roll
                    if last_states[i] and MIN_MIDDLE_MS <= off_length <= MAX_MIDDLE_MS:
                        saw_middles[i] = True
                        # Note: we don't reset anything yet — we just remember we saw
                        # a valid inter-hit gap

                # Reset the off-period tracker since we're currently seeing activity
                off_starts[i] = 0

                # ───────────────────────────────────────
                # Rising edge: idle → active  (start of bell hit)
                # ───────────────────────────────────────
                if not last_states[i]:
                    last_states[i] = True                # Latch ON
                    start_times[i] = now                 # Record when the whole event began
                    leds[i].value = 0                    # LED ON (visual feedback)
                    bell_num = i + 1                     # Human-readable bell number (1–8)

                    # Notify all registered handlers that a new hit sequence started
                    # rising edge
                    for handler in handlers:
                        handler.on_start(bell_num)

            # ────────────────────────────────────────────────
            # CASE 2: Input is currently INACTIVE (released / high)
            # ────────────────────────────────────────────────
            else:
                # Detect falling edge of the raw signal (active → idle)
                # This marks the potential beginning of an inter-pulse gap
                if last_raw[i]:                      # was active last scan → now idle
                    off_starts[i] = now              # Start counting how long this off period lasts

            # ────────────────────────────────────────────────
            # LATCH TIMEOUT / END-OF-EVENT DETECTION
            # If the bell is still considered "latched on" (in a hit sequence),
            # but we haven't seen activity for a long time → consider the sequence finished
            # ────────────────────────────────────────────────
            if last_states[i]:
                elapsed_since_pulse = ticks_diff(now, last_pulse_times[i])

                if elapsed_since_pulse > LATCH_TIMEOUT_MS:
                    # Full event duration (from first contact to timeout)
                    overall_length = ticks_diff(now, start_times[i])
                    bell_num = i + 1
                    # ──────────────────────────────
                    #  ← Add this: calculate velocity
                    velocity = duration_to_velocity(overall_length)
                    print(f"{timestamp()} Channel {i+1}: overall {overall_length} ms → velocity {velocity}")
                    extra = f"overall: {overall_length} ms  vel:{velocity}"
                    # ──────────────────────────────
                    # Decide how to report the event
                    if saw_middles[i]:
                        # At least one valid middle gap → treat as multiple hits
                        hit_counts[i] += 1                   # Usually increment by 1 per clean sequence
                        extra = f"{timestamp()} MIDDLE      overall: {overall_length} ms"
                    else:
                        # No middle gap detected → single hit (or very fast buzz)
                        # Many systems still report this as hit #1
                        hit_counts[i] += 1
                        extra = f"{timestamp()} --------------   overall: {overall_length} ms"
                        
                    for handler in handlers:
                        handler.on_end(bell_num, hit_counts[i], extra, velocity=velocity)


                    # ───────────────────────────────
                    # Reset all state for this channel
                    # ───────────────────────────────
                    last_states[i]  = False
                    saw_middles[i]  = False
                    off_starts[i]   = 0
                    leds[i].value   = 1                      # LED OFF

            # ────────────────────────────────────────────────
            # Remember raw value for next scan (edge detection)
            # ────────────────────────────────────────────────
            last_raw[i] = raw

        # ────────────────────────────────────────────────
        # Throttle the scanning loop to avoid 100% CPU usage
        # SCAN_INTERVAL_MS is usually 1–10 ms depending on required latency
        # ────────────────────────────────────────────────
        await asyncio.sleep_ms(SCAN_INTERVAL_MS)
async def blink_status_led():
    while True:
        status_led.value = 1
        await asyncio.sleep_ms(490)
        status_led.value = 0
        await asyncio.sleep_ms(10)

async def main():
    for led in leds:
        led.value = 1
    print("Starting tasks...")
    scanner_task = asyncio.create_task(input_scanner())
    status_task = asyncio.create_task(blink_status_led())
    await asyncio.gather(scanner_task, status_task)

# ------------------- Run -------------------
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopped by user")
finally:
    ir_pwm.duty_cycle = 0
    tx_led.value = 0
    status_led.value = 1
    for led in leds:
        led.value = 1
    if log_file:
        log_file.close()
        print("Log closed")
    print("Cleanup complete")