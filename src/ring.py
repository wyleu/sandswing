"""
ring.py
=======
The basic 'I'm ringing' behaviour, run by concentrator.py after its
startup tests and settle.  It owns no pins: concentrator hands it the
sense inputs, the laser PWMs and the heads found by the survey.

For each head: hold its laser on (at its laser_duty), learn its resting
level, and send USB MIDI on the change off rest.  Idle is not a note.

Zynthian is the USB host and the power source. It transposes down one
octave, so these note numbers are sent as they stand.

    bell 1..8: 65, 64, 62, 60, 58, 57, 55, 53
    channel 1 (adafruit out_channel 0)

Does not sweep the orange threshold wire. Does not paint pixels: tripped()
feeds panel.panel_frame via concentrator.
"""

NOTES = (65, 64, 62, 60, 58, 57, 55, 53)
VEL_ON = 100
VEL_OFF = 0
FULL = 65535
HEADS = ("head_new", "head_old")


def open_midi():
    """USB MIDI out, or None (no port / library missing)."""
    try:
        import usb_midi
        import adafruit_midi
    except ImportError:
        print("USB MIDI: not available")
        return None
    ports = usb_midi.ports
    out = ports[1] if len(ports) > 1 else (ports[0] if ports else None)
    if out is None:
        print("USB MIDI: no port")
        return None
    print("USB MIDI ch 1")
    return adafruit_midi.MIDI(midi_out=out, out_channel=0)


def _note_msg(note, on):
    from adafruit_midi.note_on import NoteOn
    from adafruit_midi.note_off import NoteOff
    return NoteOn(note, VEL_ON) if on else NoteOff(note, VEL_OFF)


class Ring:
    def __init__(self, inputs, lasers, midi=None, notes=NOTES, send_fn=None):
        self.inputs = inputs
        self.lasers = lasers
        self.midi = midi
        self.notes = notes
        self._send_fn = send_fn          # tests / alternative transports
        self.n = min(len(inputs), len(lasers), len(notes))
        self.heads = {}                  # ch -> "head_new" / "head_old"
        self.rest_held = {}
        self.prev_held = {}
        self.sounding = [False] * self.n
        self.strokes = [0] * self.n
        self.running = False

    def start(self, kinds, duty=None):
        """kinds: {ch: kind} from the survey.  Lasers on for heads only,
        rest level learnt now (call after the settle)."""
        self.stop()
        duty = duty or {}
        self.heads = dict((c, k) for c, k in kinds.items()
                          if k in HEADS and c < self.n)
        for ch in range(self.n):
            self.lasers[ch].duty_cycle = int(duty.get(ch, FULL)) if ch in self.heads else 0
        for ch in self.heads:
            held = not self.inputs[ch].value
            self.rest_held[ch] = held
            self.prev_held[ch] = held
        self.running = True
        print("RINGING", [(c + 1, k, self.notes[c]) for c, k in sorted(self.heads.items())])

    def _send(self, ch, on):
        note = self.notes[ch]
        if self._send_fn is not None:
            self._send_fn(ch, note, on)
        elif self.midi is not None:
            self.midi.send(_note_msg(note, on))
        print("ch %d %s %d%s" % (ch + 1, "ON" if on else "OFF", note,
                                 "" if (self.midi or self._send_fn) else " (no midi)"))

    def poll(self):
        if not self.running:
            return
        for ch in self.heads:
            held = not self.inputs[ch].value
            if held != self.prev_held[ch]:
                trip = held != self.rest_held[ch]
                if trip and not self.sounding[ch]:
                    self.sounding[ch] = True
                    self.strokes[ch] += 1
                    self._send(ch, True)
                elif (not trip) and self.sounding[ch]:
                    self.sounding[ch] = False
                    self._send(ch, False)
                self.prev_held[ch] = held

    def tripped(self):
        return set(ch for ch in self.heads if self.sounding[ch])

    def stop(self):
        """Notes off, lasers off (before a rescan or at exit)."""
        for ch in range(self.n):
            if self.sounding[ch]:
                self.sounding[ch] = False
                self._send(ch, False)
            self.lasers[ch].duty_cycle = 0
        self.running = False

    def status(self):
        return {"running": self.running,
                "heads": dict((c + 1, k) for c, k in self.heads.items()),
                "strokes": list(self.strokes), "sounding": sorted(self.tripped())}


if __name__ == "__main__" or __name__ == "<module>":
    # Standalone: concentrator.py does the setup, then rings.
    import concentrator
    concentrator.run()
