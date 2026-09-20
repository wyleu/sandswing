import asyncio
import time
from machine import Pin, PWM

# ------------------- Config -------------------

# At top, after imports — no need to reload config.json

print("From boot.py globals:")
print("enabled_formats:", enabled_formats)   # should print the list now

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

# ------------------- Setup -------------------
inputs = [Pin(INPUT_BASE + i, Pin.IN) for i in range(8)]  # external pull-ups assumed
leds = [Pin(OUTPUT_BASE + i, Pin.OUT) for i in range(8)]
tx_led = Pin(TX_LED_PIN, Pin.OUT)
status_led = Pin(STATUS_LED_PIN, Pin.OUT)
ir_pwm = PWM(Pin(IR_LED_PIN))
ir_pwm.freq(CARRIER_FREQ)
ir_pwm.duty_u16(DUTY_33)


midi_out = None
midi_channel = 0
midi_note_base = 60
midi_velocity = 100
midi_note_dur = 500

if "midi" in enabled_formats:
    midi_conf = output_conf.get("midi", {})
    if midi_conf.get("enabled", False):
        try:
            midi_channel = midi_conf.get("channel", 0)
            midi_note_base = midi_conf.get("note_base", 60)
            midi_velocity = midi_conf.get("velocity", 100)
            midi_note_dur = midi_conf.get("note_duration_ms", 500)

            # Create USB MIDI device (simple single-port)
            midi_out = usb.device.midi.MidiOut()  # or usb.device.midi.MIDIInterface() if different API
            print("USB MIDI enabled (channel {}, base note {})".format(midi_channel+1, midi_note_base))
        except Exception as e:
            print("USB MIDI setup failed:", e)
            midi_out = None

abell_uart = None
if "abell" in enabled_formats:
    abell_conf = output_conf.get("abell", {})
    if abell_conf.get("enabled", False):
        try:
            uart_id = abell_conf.get("uart_id", 0)
            tx_pin = abell_conf.get("tx_pin", 0)
            baud = abell_conf.get("baud", 9600)
            abell_uart = UART(uart_id, baudrate=baud, tx=Pin(tx_pin))
            print("abell ASCII trigger on UART{} TX GP{}".format(uart_id, tx_pin))
        except Exception as e:
            print("abell UART setup failed:", e)
            
log_file = None
log_prefix = "bell_log_"
if "log" in enabled_formats:
    log_conf = output_conf.get("log", {})
    if log_conf.get("enabled", False):
        log_prefix = log_conf.get("filename_prefix", "bell_log_")
        ts = time.localtime()
        filename = "{}{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.txt".format(
            log_prefix, ts[0], ts[1], ts[2], ts[3], ts[4], ts[5])
        try:
            log_file = open(filename, "a")
            log_file.write("Bell detector log started at {}\n".format(time.localtime()))
            print("Logging to:", filename)
        except OSError as e:
            print("Log file open failed:", e)
            log_file = None

tx_led.value(1)
status_led.value(1)  # start ON
print("Async IR Bell Detector Ready (Latch Mode)")
print(f"Carrier: {CARRIER_FREQ} Hz @ ~33%")
print(f"Latch timeout: {LATCH_TIMEOUT_MS} ms")
print(f"Middle off range: {MIN_MIDDLE_MS}-{MAX_MIDDLE_MS} ms")
print(f"LED outputs: INVERTED (OFF when latched, ON when idle)")
print(f"Status LED: GP{STATUS_LED_PIN} blinking ~1 Hz")
print("Watch logs for timestamps, pulse lengths, and off periods to tune timeouts")

# ------------------- State -------------------
last_states = [False] * 8      # latch state
last_raw = [False] * 8         # for edge detect
last_pulse_times = [0] * 8     # time of last True reading
off_starts = [0] * 8           # start of current off (for measuring gaps)
start_times = [0] * 8          # start of latch period
saw_middles = [False] * 8      # flag if middle seen in this period
hit_counts = [0] * 8

# ------------------- Coroutines -------------------
async def unlatch_inputs():
    """Run once at startup to clear any RP2040 latches"""
    print("Clearing potential latches...")
    for i in range(8):
        pin_num = INPUT_BASE + i
        temp = Pin(pin_num, Pin.OUT)
        temp.value(0)
        await asyncio.sleep_ms(1)
        temp = Pin(pin_num, Pin.IN)
    print("Unlatch complete")

async def input_scanner():
    """Poll inputs, manage latches, measure pulses/offs"""
    while True:
        now = time.ticks_ms()
        changed = False
        for i in range(8):
            raw = not inputs[i].value()  # active-high assumption
            
            if raw:
                # Retrigger/update pulse time
                last_pulse_times[i] = now
                
                # If coming from off, measure off length
                if off_starts[i] != 0:
                    off_length = time.ticks_diff(now, off_starts[i])
                    print(f"Bell {i+1} off length: {off_length} ms")
                    if last_states[i] and MIN_MIDDLE_MS <= off_length <= MAX_MIDDLE_MS:
                        saw_middles[i] = True
                        print(f"Bell {i+1} MIDDLE STRIP detected ({off_length} ms)")
                
                # Reset off tracker
                off_starts[i] = 0
                
                # Set latch if not already
                if not last_states[i]:
                    last_states[i] = True
                    start_times[i] = now
                    print(f"Bell {i+1} LATCH SET at {now} ms")
                    leds[i].value(0)  # INVERTED: OFF when latched
                    changed = True
            
            else:
                # If falling edge (was True, now False)
                if last_raw[i]:
                    off_starts[i] = now
            
            # Check for latch timeout
            if last_states[i]:
                elapsed_since_pulse = time.ticks_diff(now, last_pulse_times[i])
                if elapsed_since_pulse > LATCH_TIMEOUT_MS:
                    overall_length = time.ticks_diff(now, start_times[i])
                    print(f"Bell {i+1} LATCH RESET at {now} ms, overall pulse length: {overall_length} ms")
                    if saw_middles[i]:
                        hit_counts[i] += 1
                        bell_num = i + 1

                        print(f"Bell {bell_num} DETECTED (with middle, count: {hit_counts[i]})")

                        # abell ASCII '1'-'8' trigger
                        if abell_uart:
                            abell_uart.write(bytes([ord('0') + bell_num]))  # sends b'1' to b'8'
                            print(f"abell sent: {bell_num}")
                            
                    # USB MIDI Note On
                        if midi_out:
                            note = midi_note_base + i
                            msg_on = bytes([0x90 + midi_channel, note, midi_velocity])   # Note On
                            midi_out.write(msg_on)  # or midi_out.send(msg_on) if API differs
                            print(f"MIDI Note On ch{midi_channel+1} note{note} vel{midi_velocity}")

                            # Optional: schedule Note Off after duration (simple delay for now; use asyncio timer for non-block)
                            # For better: create async task per note-off
                            async def note_off_task():
                                await asyncio.sleep_ms(midi_note_dur)
                                msg_off = bytes([0x80 + midi_channel, note, 0])  # Note Off
                                midi_out.write(msg_off)
                                print(f"MIDI Note Off ch{midi_channel+1} note{note}")
                            asyncio.create_task(note_off_task())

                        # Log entry
                        if log_file:
                            now_str = "{:02d}:{:02d}:{:02d}".format(time.localtime()[3:6])
                            log_file.write(f"{now_str} Bell {bell_num} DETECTED (count {hit_counts[i]})\n")
                            log_file.flush()                        
                                
                else:
                    print(f"Bell {i+1} pulse WITHOUT middle (ignored)")
                last_states[i] = False
                saw_middles[i] = False
                off_starts[i] = 0
                leds[i].value(1)  # INVERTED: ON when idle
                changed = True
            
            last_raw[i] = raw
        
        if changed:
            print(f"Current hits: {hit_counts}")
        
        await asyncio.sleep_ms(SCAN_INTERVAL_MS)

async def blink_status_led():
    """Independent ~1 Hz blinker (490 ms on / 10 ms off)"""
    while True:
        status_led.value(1)
        await asyncio.sleep_ms(490)
        status_led.value(0)
        await asyncio.sleep_ms(10)

async def main():
    # Startup sequence
    await unlatch_inputs()
    # Turn all bell LEDs ON at startup (idle state – inverted)
    for led in leds:
        led.value(1)
    print("Starting tasks...")
    scanner_task = asyncio.create_task(input_scanner())
    status_task = asyncio.create_task(blink_status_led())
    # Keep event loop alive
    await asyncio.gather(scanner_task, status_task)

# ------------------- Entry point -------------------
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopped by user")
finally:
    ir_pwm.duty_u16(0)
    if log_file:
        log_file.close()
        print("Log closed")
    # MIDI auto-closes on deinit; no explicit close needed usually
    tx_led.value(0)
    status_led.value(1)
    for led in leds:
        led.value(1)  # idle ON on exit
    print("Cleanup complete")