"""
ring.py
=======
Standard running program for the concentrator.

code.py stays the launcher. Point settings.json -> startup.program here.
concentrator.py stays the setup program. This file does not replace it.

What it does
------------
1. Pulse each laser and read that channel's 4051 port. A head is fitted
   only if the port steps. An empty port does not light its laser.
2. Sample the sense wire. Steady low is an old head (dark at rest, rises
   on a reflection). Steady high is a new head (trip is the fall).
3. Hold the fitted lasers on, paint the status pixel green, and send USB
   MIDI on the change off rest. Idle is not a note.

Zynthian is the USB host and the power source. It transposes down one
octave, so these note numbers are sent as they stand.

    bell 1..8: 65, 64, 62, 60, 58, 57, 55, 53
    channel 1 (adafruit out_channel 0)

Does not sweep the orange threshold wire. Does not store the label in nvm.
"""

import time
import board
import digitalio
import pwmio
import analogio
import neopixel
import usb_midi
import adafruit_midi
from adafruit_midi.note_on import NoteOn
from adafruit_midi.note_off import NoteOff

from pins_from_settings import load_pinmap

CONFIG_FILE = "settings.json"
NOTES = (65, 64, 62, 60, 58, 57, 55, 53)
VEL_ON = 100
VEL_OFF = 0
PULSE = 65535
SETTLE_S = 0.05
PRESENT_DELTA = 2000
CLASS_S = 0.3

# Strip is GRB, matching concentrator.py.
STATUS_INIT = (16, 16, 0)   # amber while checking
STATUS_RUN = (40, 0, 0)     # green: ringing
REST_OLD = (0, 0, 4)        # blue
REST_NEW = (4, 0, 0)        # green
RESPONSE = (0, 48, 0)       # red
OFF = (0, 0, 0)


def load_settings():
    import json
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def open_midi():
    ports = usb_midi.ports
    out = ports[1] if len(ports) > 1 else (ports[0] if ports else None)
    if out is None:
        print("USB MIDI: no port")
        return None
    midi = adafruit_midi.MIDI(midi_out=out, out_channel=0)
    print("USB MIDI ch 1")
    return midi


class Ring:
    def __init__(self, cfg):
        pinmap = load_pinmap(cfg)
        self.sense_gps = list(pinmap["sense_gp"])
        self.laser_gps = list(pinmap["laser_gp"])
        self.n = min(len(self.sense_gps), len(self.laser_gps), len(NOTES))
        mux = cfg.get("pins", {}).get("adc_mux", {})
        self.mux_y = list(mux.get("laser_y", [0, 1, 2, 3]))
        self.strip = neopixel.NeoPixel(
            getattr(board, "GP%d" % pinmap["neopixel_gp"]),
            self.n + 2,
            brightness=0.3,
            auto_write=False,
        )
        self.strip.fill(OFF)
        self.strip[0] = STATUS_INIT
        self.strip.show()
        self.midi = open_midi()
        self.lasers = []
        self.addr = []
        self.adc = None
        self.inputs = []
        self.fitted = []
        self.kind = [None] * self.n
        self.rest_held = [None] * self.n
        self.prev_held = [False] * self.n
        self.sounding = [False] * self.n
        self._claim_mux(pinmap, mux)
        self._find_heads()
        self._claim_sense()
        self._classify()
        self._lasers_on_fitted()
        self.strip[0] = STATUS_RUN
        self.strip.show()
        print("RINGING", [(i + 1, self.kind[i], NOTES[i]) for i in self.fitted])

    def _claim_mux(self, pinmap, mux):
        for gp in self.laser_gps[:self.n]:
            self.lasers.append(
                pwmio.PWMOut(getattr(board, "GP%d" % gp), frequency=1000, duty_cycle=0)
            )
        for key in ("a", "b", "c"):
            gp = mux.get(key)
            if gp is None:
                continue
            pin = digitalio.DigitalInOut(getattr(board, "GP%d" % gp))
            pin.direction = digitalio.Direction.OUTPUT
            pin.value = False
            self.addr.append(pin)
            
        adc_gp = mux.get("common", 26)
        self.adc = analogio.AnalogIn(getattr(board, "GP%d" % adc_gp))
        print("presence ADC GP%d" % adc_gp)

    def _select(self, port):
        for bit in range(len(self.addr)):
            self.addr[bit].value = bool(port & (1 << bit))
        time.sleep(0.002)

    def _read(self, port):
        self._select(port)
        return self.adc.value

    def _find_heads(self):
        for ch in range(self.n):
            port = self.mux_y[ch] if ch < len(self.mux_y) else ch
            self.lasers[ch].duty_cycle = 0
            time.sleep(SETTLE_S)
            dark = self._read(port)
            self.lasers[ch].duty_cycle = PULSE
            time.sleep(SETTLE_S)
            lit = self._read(port)
            self.lasers[ch].duty_cycle = 0
            delta = lit - dark
            present = delta >= PRESENT_DELTA
            print("ch %d mux %d dark %d lit %d d %+d %s" % (
                ch + 1, port, dark, lit, delta, "HEAD" if present else "empty"))
            if present:
                self.fitted.append(ch)
        print("fitted", [i + 1 for i in self.fitted])

    def _claim_sense(self):
        for gp in self.sense_gps[:self.n]:
            pin = digitalio.DigitalInOut(getattr(board, "GP%d" % gp))
            pin.direction = digitalio.Direction.INPUT
            pin.pull = digitalio.Pull.UP
            self.inputs.append(pin)

    def _classify(self):
        time.sleep(CLASS_S)
        for i in self.fitted:
            held = not self.inputs[i].value
            self.kind[i] = "old" if held else "new"
            self.rest_held[i] = held
            self.prev_held[i] = held
            print("ch %d %s rest_%s note %d" % (
                i + 1, self.kind[i], "low" if held else "high", NOTES[i]))

    def _lasers_on_fitted(self):
        for ch in range(self.n):
            self.lasers[ch].duty_cycle = PULSE if ch in self.fitted else 0

    def _send(self, i, on):
        note = NOTES[i]
        if self.midi is None:
            print("ch %d %s %d (no midi)" % (i + 1, "ON" if on else "OFF", note))
            return
        if on:
            self.midi.send(NoteOn(note, VEL_ON))
        else:
            self.midi.send(NoteOff(note, VEL_OFF))
        print("ch %d %s %d" % (i + 1, "ON" if on else "OFF", note))

    def _pixel(self, i, held):
        if i not in self.fitted or self.kind[i] is None:
            self.strip[i + 1] = OFF
            return
        if held != self.rest_held[i]:
            self.strip[i + 1] = RESPONSE
        elif self.kind[i] == "old":
            self.strip[i + 1] = REST_OLD
        else:
            self.strip[i + 1] = REST_NEW

    def poll(self):
        for i in self.fitted:
            held = not self.inputs[i].value
            if held != self.prev_held[i]:
                trip = held != self.rest_held[i]
                if trip and not self.sounding[i]:
                    self.sounding[i] = True
                    self._send(i, True)
                elif (not trip) and self.sounding[i]:
                    self.sounding[i] = False
                    self._send(i, False)
                self.prev_held[i] = held
            self._pixel(i, held)
        self.strip.show()

    def loop(self):
        while True:
            self.poll()
            time.sleep(0.01)

    def close(self):
        for i in range(self.n):
            if self.sounding[i]:
                self._send(i, False)
            if i < len(self.lasers):
                self.lasers[i].duty_cycle = 0
        self.strip.fill(OFF)
        self.strip.show()


def main():
    app = Ring(load_settings())
    try:
        app.loop()
    except KeyboardInterrupt:
        print("Stopped")
    finally:
        app.close()


if __name__ == "__main__" or __name__ == "<module>":
    main()
