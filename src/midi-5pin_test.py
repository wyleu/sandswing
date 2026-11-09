# midi-5pin_test.py
# Minimal CircuitPython test for 5-pin MIDI out on GPIO17 (Pico 2 W concentrator).
# Wiring (already done): GPIO17 -- 10R -- MIDI DIN pin 5; 3.3V -- 22R -- MIDI DIN pin 4.
# Sends a short scale and a basic SysEx identity so you can verify the port with a USB-MIDI interface or sanddisp.

import board
import digitalio
import time
import usb_midi
import adafruit_midi
from adafruit_midi.note_on import NoteOn
from adafruit_midi.note_off import NoteOff
from adafruit_midi.control_change import ControlChange
from adafruit_midi.system_exclusive import SystemExclusive

# ---- config (hard-coded for the test; later from settings.json) ----
MIDI_CHANNEL = 1          # 1-16
NOTE_BASE = 60            # middle C
NOTE_RANGE = 12           # one octave
NOTE_OFF_MODE = "explicit"  # "explicit" or "velocity0"
SYSEX_ENABLED = True
TX_PIN = board.GP16

# UART MIDI out (31250 baud is the MIDI standard)
import busio
uart = busio.UART(tx=TX_PIN, rx=None, baudrate=31250, timeout=0.001)
midi = adafruit_midi.MIDI(midi_out=uart, out_channel=MIDI_CHANNEL - 1)

print("5-pin MIDI test on GPIO17")
print(f"Channel {MIDI_CHANNEL}, notes {NOTE_BASE}..{NOTE_BASE+NOTE_RANGE-1}")

def send_note(note, velocity=100, duration=0.2):
    midi.send(NoteOn(note, velocity))
    time.sleep(duration)
    if NOTE_OFF_MODE == "explicit":
        midi.send(NoteOff(note, 0))
    else:
        midi.send(NoteOn(note, 0))  # velocity 0 as Note Off

# Short ascending scale
for i in range(NOTE_RANGE):
    send_note(NOTE_BASE + i)
    time.sleep(0.05)

# All-notes-off style CC
midi.send(ControlChange(123, 0))  # All Notes Off

# Optional SysEx identity (non-commercial manufacturer ID 0x7D)
if SYSEX_ENABLED:
    # Simple "Sandswing concentrator" identity
    identity = bytes([0x7D, 0x53, 0x53, 0x01])  # 0x7D + "SS" + version
    midi.send(SystemExclusive([0x7D], [0x53, 0x53, 0x01]))
    print("SysEx identity sent")

print("Test sequence finished. Check receiver (sanddisp / USB-MIDI).")
# Keep alive briefly so the last bytes flush
time.sleep(0.5)
