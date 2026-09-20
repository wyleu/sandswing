import asyncio
import time
import sys
import supervisor
from adafruit_ticks import ticks_diff, ticks_ms


print("running on", sys.implementation.name)

if sys.implementation.name == "micropython":
    from machine import Pin, PWM, UART
    import usb.device.midi  # Assuming installed via mip
    
elif sys.implementation.name == "circuitpython":
    import board
    import digitalio
    import pwmio
    # import busio   # for UART if still using hardware (but prefer usb_cdc.data for abell)
    import adafruit_ticks
    
    import usb_midi
    import adafruit_midi
    from adafruit_midi.note_on  import NoteOn
    from adafruit_midi.note_off import NoteOff
    from adafruit_midi.control_change import ControlChange


import json
import os


# ------------------- Config -------------------
MODE = "latch_detect_with_middle"
print('IR Bell Detector v2 - Latch Mode')
LATCH_TIMEOUT_MS = 100  # > max middle off; adjust based on logs
MIN_MIDDLE_MS = 10      # min off for middle strip (ignore noise < this)
MAX_MIDDLE_MS = 80      # max off for middle strip (< latch timeout)
SCAN_INTERVAL_MS = 10
INPUT_BASE = 14  # GP14–21
OUTPUT_BASE = 0  # GP0–7
IR_LED_PIN = 13
TX_LED_PIN = 12
STATUS_LED_PIN = 22
DUTY_33 = 21845
CARRIER_FREQ = 38000

log_file = None
midi_out = None
midi_channel = 0
midi_note_base = 60
midi_velocity = 100
midi_note_dur = 500

# ------------------- Setup -------------------


# micropython
# inputs = [Pin(INPUT_BASE + i, Pin.IN) for i in range(8)]  # external pull-ups assumed
# leds = [Pin(OUTPUT_BASE + i, Pin.OUT) for i in range(8)]

# Assuming your constants are still defined the same way
INPUT_BASE  = 14   # GP14 – GP21
OUTPUT_BASE = 0    # GP0  – GP7

# Input pins (GP14 to GP21)
inputs = []
for i in range(8):
    pin = digitalio.DigitalInOut(getattr(board, f"GP{INPUT_BASE + i}"))
    pin.direction = digitalio.Direction.INPUT
    # If you need internal pull-up (only if external pull-ups are NOT present):
    # pin.pull = digitalio.Pull.UP
    # But you wrote "external pull-ups assumed" → so we leave pull=None
    inputs.append(pin)

# Output pins (GP0 to GP7)
leds = []
for i in range(8):
    pin = digitalio.DigitalInOut(getattr(board, f"GP{OUTPUT_BASE + i}"))
    pin.direction = digitalio.Direction.OUTPUT
    leds.append(pin)


# tx_led = Pin(TX_LED_PIN, Pin.OUT)
tx_led = digitalio.DigitalInOut(board.GP12)
tx_led.direction = digitalio.Direction.OUTPUT
# status_led = Pin(STATUS_LED_PIN, Pin.OUT)
status_led = digitalio.DigitalInOut(board.GP22)
status_led.direction = digitalio.Direction.OUTPUT

# ir_pwm = PWM(Pin(IR_LED_PIN))
ir_pwm = pwmio.PWMOut(board.GP13, frequency=CARRIER_FREQ, duty_cycle=DUTY_33)
# Duty cycle is 0–65535 (same as u16)
# To turn off later: ir_pwm.duty_cycle = 0
# Micropython
# ir_pwm.freq(CARRIER_FREQ)
# ir_pwm.duty_u16(DUTY_33)

tx_led.value = 1 
status_led.value = 1  # start ON
print("Async IR Bell Detector Ready (Latch Mode)")
print(f"Carrier: {CARRIER_FREQ} Hz @ ~33%")
print(f"Latch timeout: {LATCH_TIMEOUT_MS} ms")
print(f"Middle off range: {MIN_MIDDLE_MS}-{MAX_MIDDLE_MS} ms")
print(f"LED outputs: INVERTED (OFF when latched, ON when idle)")
print(f"Status LED: GP{STATUS_LED_PIN} blinking ~1 Hz")
print("Watch logs for timestamps, pulse lengths, and off periods to tune timeouts")

# Load config (assuming globals from boot.py or reload here if not)
# For this example, reload to make it self-contained
CONFIG_FILE = "settings.json"
config = {}
try:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    print("Loaded:", CONFIG_FILE)
    
except Exception as e:
    print("Config load error:", e)

wifi_ssid       = config["wifi"]["ssid"]
midi_channel    = config["output"]["midi"]["channel"]
enabled_formats = config["output"]["enabled_formats"]
test_enabled    = config["output"]["test"]["enabled"]
output_conf    = config["output"]
startup_program = config["startup"]["program"]


# Setup outputs (UART, MIDI, log)
# abell_uart = None
# if "abell" in enabled_formats:
#     abell_conf = output_conf.get("abell", {})
#     if abell_conf.get("enabled", False):
#         try:
#             uart_id = abell_conf.get("uart_id", 0)
#             tx_pin = abell_conf.get("tx_pin", 0)
#             baud = abell_conf.get("baud", 9600)
#             abell_uart = UART(uart_id, baudrate=baud, tx=Pin(tx_pin))
#             print(f"abell ASCII on UART{uart_id} TX GP{tx_pin}")
#         except Exception as e:
#             print("abell setup failed:", e)


# ------------------- Output Handlers -------------------
class OutputHandler:
    def __init__(self, name, enabled):
        self.name = name
        self.enabled = enabled

    def on_detection(self, bell_num, count, extra_info=None):
        pass

class ConsoleHandler(OutputHandler):
    def __init__(self):
        super().__init__("console", True)

    def on_detection(self, bell_num, count, extra_info=None):
        print(f"Bell {bell_num} DETECTED (with middle, count: {count})")
        if extra_info:
            print(extra_info)

class LogHandler(OutputHandler):
    def __init__(self, log_file):
        super().__init__("log", log_file is not None)
        self.log_file = log_file

    def on_detection(self, bell_num, count, extra_info=None):
        if not self.enabled or not self.log_file:
            return
        now_str = f"{time.localtime()[3]:02d}:{time.localtime()[4]:02d}:{time.localtime()[5]:02d}"
        msg = f"{now_str} Bell {bell_num} DETECTED (count {count})"
        if extra_info:
            msg += f" - {extra_info}"
        self.log_file.write(msg + "\n")
        self.log_file.flush()

# class AbellHandler(OutputHandler):
#     def __init__(self, uart):
#         super().__init__("abell", uart is not None)
#         self.uart = uart
# 
#     def on_detection(self, bell_num, count, extra_info=None):
#         if not self.enabled or not self.uart:
#             return
#         self.uart.write(bytes([ord('0') + bell_num]))
#         print(f"abell sent: {bell_num}")

class MIDIHandler(OutputHandler):
    def __init__(self, channel, note_base, velocity, note_dur):
        super().__init__("midi", True)
        self.midi = adafruit_midi.MIDI(
            midi_out=usb_midi.ports[1],
            out_channel=channel   # 0 = ch1, etc.
        )
        self.note_base = note_base
        self.velocity = velocity
        self.note_dur = note_dur

    def on_detection(self, bell_num, count, extra_info=None):
        note = self.note_base + (bell_num - 1)
        print(f"MIDI sending NoteOn  ch{self.midi.out_channel+1} note{note} vel{self.velocity}")

        self.midi.send(NoteOn(note, self.velocity))

        # Schedule note-off async so it doesn't block scanner
        async def note_off_task():
            await asyncio.sleep(self.note_dur / 1000.0)  # convert ms → seconds
            self.midi.send(NoteOff(note, 0))
            print(f"MIDI NoteOff ch{self.midi.out_channel+1} note{note}")

        asyncio.create_task(note_off_task())

# Create active handlers
handlers = [
    ConsoleHandler()
]
if log_file:
    handlers.append(LogHandler(log_file))
# if abell_uart:
#     handlers.append(AbellHandler(abell_uart))
if midi_out:
    handlers.append(MIDIHandler(midi_out, midi_channel, midi_note_base, midi_velocity, midi_note_dur))

# ------------------- State -------------------
last_states = [False] * 8      # latch state
last_raw = [False] * 8         # for edge detect
last_pulse_times = [0] * 8     # time of last True reading
off_starts = [0] * 8           # start of current off (for measuring gaps)
start_times = [0] * 8          # start of latch period
saw_middles = [False] * 8      # flag if middle seen in this period
hit_counts = [0] * 8



if "midi" in enabled_formats:
    midi_conf = output_conf.get("midi", {})
    if midi_conf.get("enabled", False):
        try:
            midi_channel = midi_conf.get("channel", 0)          # 0-based
            midi_note_base = midi_conf.get("note_base", 60)
            midi_velocity = midi_conf.get("velocity", 100)
            midi_note_dur = midi_conf.get("note_duration_ms", 500)

            handlers.append(
                MIDIHandler(
                    channel=midi_channel,
                    note_base=midi_note_base,
                    velocity=midi_velocity,
                    note_dur=midi_note_dur
                )
            )
            print(f"USB MIDI handler added (ch {midi_channel+1}, base {midi_note_base})")
        except Exception as e:
            print("MIDI handler setup failed:", e)


log_prefix = "bell_log_"
if "log" in enabled_formats:
    log_conf = output_conf.get("log", {})
    if log_conf.get("enabled", False):
        log_prefix = log_conf.get("filename_prefix", "bell_log_")
        ts = time.localtime()
        filename = f"{log_prefix}{ts[0]:04d}{ts[1]:02d}{ts[2]:02d}_{ts[3]:02d}{ts[4]:02d}{ts[5]:02d}.txt"
        try:
            log_file = open(filename, "a")
            log_file.write(f"Log started at {time.localtime()}\n")
            print(f"Logging to: {filename}")
        except OSError as e:
            print("Log open failed:", e)


# ------------------- Coroutines -------------------
async def unlatch_inputs():
    print("Clearing potential latches...")
    for i in range(8):
        pin_num = INPUT_BASE + i
        #temp = Pin(pin_num, Pin.OUT)
        temp = digitalio.DigitalInOut(getattr(board, f"GP{INPUT_BASE + i}"))
        temp.direction = digitalio.Direction.OUTPUT
        
        temp.value = 0
        await asyncio.sleep_ms(1)
        temp = digitalio.DigitalInOut(getattr(board, f"GP{INPUT_BASE + i}"))
        temp.direction = digitalio.Direction.INPUT
        #temp = Pin(pin_num, Pin.IN)
    print("Unlatch complete")

# Remove or comment out:
# DEBUG_BELL_ONLY = None   # no longer needed

# In input_scanner() — stripped down, no per-bell debug spam
async def input_scanner():
    while True:
        now = supervisor.ticks_ms()   # or ticks_ms() if imported directly

        for i in range(8):
            raw = not inputs[i].value  # assuming active-low after inversion

            if raw:
                last_pulse_times[i] = now

                if off_starts[i] != 0:
                    off_length = ticks_diff(now, off_starts[i])
                    if last_states[i] and MIN_MIDDLE_MS <= off_length <= MAX_MIDDLE_MS:
                        saw_middles[i] = True
                        # Optional: keep one line for tuning visibility
                        # print(f"Bell {i+1} middle off detected: {off_length} ms")

                off_starts[i] = 0

                if not last_states[i]:
                    last_states[i] = True
                    start_times[i] = now
                    leds[i].value = 0  # latched = OFF (inverted)
                    # print(f"Bell {i+1} latched")   # ← comment out after tuning

            else:
                if last_raw[i]:
                    off_starts[i] = now

            if last_states[i]:
                elapsed_since_pulse = ticks_diff(now, last_pulse_times[i])
                if elapsed_since_pulse > LATCH_TIMEOUT_MS:
                    overall_length = ticks_diff(now, start_times[i])

                    if saw_middles[i]:
                        hit_counts[i] += 1
                        extra = f"overall pulse: {overall_length} ms, middle off in range"
                        print(f"Bell {i+1} DETECTED (count {hit_counts[i]}) - {extra}")
                        for handler in handlers:
                            handler.on_detection(i+1, hit_counts[i], extra_info=extra)
                    else:
                        print(f"Bell {i+1} latch timeout WITHOUT middle → ignored")

                    last_states[i] = False
                    saw_middles[i] = False
                    off_starts[i] = 0
                    leds[i].value = 1  # idle = ON (inverted)

            last_raw[i] = raw

        await asyncio.sleep_ms(SCAN_INTERVAL_MS)

async def blink_status_led():
    while True:
        status_led.value = 1
        await asyncio.sleep_ms(490)
        status_led.value = 0
        await asyncio.sleep_ms(10)
        
async def test_emitter():
    """Periodically send test messages to configured handlers"""
    if not TEST_ENABLED:
        return

    print(f"Test emitter started - mode: {TEST_MODE}, interval: {TEST_INTERVAL_MS} ms")

    test_counter = 0

    while True:
        test_counter += 1
        print(f"Test cycle #{test_counter}")

        is_all = TEST_MODE == "all"

        # MIDI test
        if is_all or TEST_MODE == "midi":
            if midi_out and "midi" in enabled_formats:
                note = test_midi_note
                ch = midi_channel
                vel = test_midi_vel

                msg_on = bytes([0x90 + ch, note, vel])
                midi_out.write(msg_on)
                print(f"TEST MIDI Note On  ch{ch+1} note{note} vel{vel}")

                await asyncio.sleep_ms(test_midi_dur)

                msg_off = bytes([0x80 + ch, note, 0])
                midi_out.write(msg_off)
                print(f"TEST MIDI Note Off ch{ch+1} note{note}")

            else:
                print("TEST MIDI skipped - MIDI not enabled or setup failed")

#         # abell test (send ASCII 'T' or something recognisable)
#         if is_all or TEST_MODE == "abell":
#             if abell_uart and "abell" in enabled_formats:
#                 abell_uart.write(b'T')  # or b'9' or whatever your synth recognises as test
#                 print("TEST abell sent: 'T'")
#             else:
#                 print("TEST abell skipped - not enabled")

        # log test
        if is_all or TEST_MODE == "log":
            if log_file and "log" in enabled_formats:
                now_str = f"{time.localtime()[3]:02d}:{time.localtime()[4]:02d}:{time.localtime()[5]:02d}"
                log_file.write(f"{now_str} TEST LOG ENTRY #{test_counter}\n")
                log_file.flush()
                print(f"TEST log written: entry #{test_counter}")
            else:
                print("TEST log skipped - not enabled")

        # console is always visible - no need to "test" it separately

        await asyncio.sleep_ms(TEST_INTERVAL_MS)

async def main():
    # await unlatch_inputs()
    for led in leds:
        led.value = 1
    print("Starting tasks...")
    scanner_task = asyncio.create_task(input_scanner())
    status_task = asyncio.create_task(blink_status_led())
    
    tasks = [scanner_task, status_task]
    
    if test_enabled:
        test_task = asyncio.create_task(test_emitter())
        tasks.append(test_task)
    
    await asyncio.gather(*tasks)

# ------------------- Entry point -------------------
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