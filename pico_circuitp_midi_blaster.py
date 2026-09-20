import time
import usb_midi
import adafruit_midi
from adafruit_midi.note_on import NoteOn
from adafruit_midi.note_off import NoteOff

print("Simple MIDI test - should send every ~1.3 seconds")

midi = adafruit_midi.MIDI(midi_out=usb_midi.ports[1], out_channel=0)

while True:
    print("Sending NoteOn 60 vel 100")
    midi.send(NoteOn(60, 100))
    time.sleep(0.3)
    print("Sending NoteOff 60")
    midi.send(NoteOff(60, 0))
    time.sleep(1.0)