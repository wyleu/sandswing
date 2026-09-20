import asyncio
import time
from machine import Pin, PWM

# ------------------- Config -------------------
MODE = "continuous_detect"
print('main.py')
USE_DEBOUNCE = False
DEBOUNCE_MS = 20
SCAN_INTERVAL_MS = 10
INPUT_BASE = 14          # GP14–21
OUTPUT_BASE = 0          # GP0–7
IR_LED_PIN = 13
TX_LED_PIN = 12
STATUS_LED_PIN = 22      # ← New: status LED
DUTY_33 = 21845
CARRIER_FREQ = 38000

# ------------------- Setup -------------------
inputs = [Pin(INPUT_BASE + i, Pin.IN) for i in range(8)]  # external pull-ups assumed
leds = [Pin(OUTPUT_BASE + i, Pin.OUT) for i in range(8)]
tx_led = Pin(TX_LED_PIN, Pin.OUT)
status_led = Pin(STATUS_LED_PIN, Pin.OUT)               # ← New

ir_pwm = PWM(Pin(IR_LED_PIN))
ir_pwm.freq(CARRIER_FREQ)
ir_pwm.duty_u16(DUTY_33)

tx_led.value(1)
status_led.value(1)           # start ON

print("Async IR Bell Detector Ready")
print(f"Carrier: {CARRIER_FREQ} Hz @ ~33%")
print(f"Debounce: {'ON' if USE_DEBOUNCE else 'OFF'}")
print(f"LED outputs: INVERTED (ON when idle, OFF when bell detected)")
print(f"Status LED: GP{STATUS_LED_PIN} blinking 1 Hz")

# ------------------- State -------------------
last_raw = [False] * 8
last_states = [False] * 8
debounce_start = [0] * 8
hit_counts = [0] * 8

# ------------------- Coroutines -------------------
async def unlatch_inputs():
    """Run once at startup to clear any RP2350 A2 E9 latches"""
    print("Clearing potential latches...")
    for i in range(8):
        pin_num = INPUT_BASE + i
        temp = Pin(pin_num, Pin.OUT)
        temp.value(0)
        await asyncio.sleep_ms(1)
        temp = Pin(pin_num, Pin.IN)
    print("Unlatch complete")

async def input_scanner():
    """Poll inputs and update states/LEDs (inverted logic)"""
    while True:
        now = time.ticks_ms()
        changed = False
        for i in range(8):
            raw = not inputs[i].value()   # active-high assumption

            if USE_DEBOUNCE:
                if raw != last_raw[i]:
                    debounce_start[i] = now
                    last_raw[i] = raw
                elapsed = time.ticks_diff(now, debounce_start[i])
                stable_detected = raw and (elapsed >= DEBOUNCE_MS)
                stable_clear = not raw and (elapsed >= DEBOUNCE_MS)

                if stable_detected and not last_states[i]:
                    last_states[i] = True
                    hit_counts[i] += 1
                    print(f"Bell {i+1} DETECTED (count: {hit_counts[i]})")
                    leds[i].value(0)          # ← INVERTED: OFF when detected
                    changed = True
                elif stable_clear and last_states[i]:
                    last_states[i] = False
                    print(f"Bell {i+1} cleared")
                    leds[i].value(1)          # ← INVERTED: ON when idle
                    changed = True
            else:
                if raw and not last_states[i]:
                    last_states[i] = True
                    hit_counts[i] += 1
                    print(f"Bell {i+1} DETECTED (count: {hit_counts[i]})")
                    leds[i].value(0)          # ← INVERTED
                    changed = True
                elif not raw and last_states[i]:
                    last_states[i] = False
                    print(f"Bell {i+1} cleared")
                    leds[i].value(1)          # ← INVERTED
                    changed = True

        if changed:
            print(f"Current hits: {hit_counts}")

        await asyncio.sleep_ms(SCAN_INTERVAL_MS)

async def blink_status_led():
    """Independent 1 Hz blinker (10 ms on / 490 ms off)"""
    while True:
        status_led.value(1)
        await asyncio.sleep_ms(490)
        status_led.value(0)
        await asyncio.sleep_ms(10)

async def main():
    # Startup sequence
    await unlatch_inputs()

    # Turn all bell LEDs ON at startup (idle state – inverted logic)
    for led in leds:
        led.value(1)

    print("Starting tasks...")
    scanner_task = asyncio.create_task(input_scanner())
    status_task  = asyncio.create_task(blink_status_led())

    # Keep event loop alive (wait on one – others run in background)
    await asyncio.gather(scanner_task, status_task)

# ------------------- Entry point -------------------
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopped by user")
finally:
    ir_pwm.duty_u16(0)
    tx_led.value(0)
    status_led.value(1)
    for led in leds:
        led.value(1)          # all off on exit (or change to 1 if you prefer idle=ON)
    print("Cleanup complete")