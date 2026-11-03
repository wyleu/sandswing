"""
ring.py
=======
The basic 'I'm ringing' behaviour, run by concentrator.py after its
startup tests and settle.  It owns no pins: concentrator hands it the
sense inputs (read once, for each head's rest level), the laser PWMs and
the heads found by the survey, and feeds it every sense edge (edge()).

For each head: hold its laser on (at its laser_duty), learn its resting
level, and send USB MIDI on the change off rest.  Idle is not a note.

Zynthian is the USB host and the power source. It transposes down one
octave, so these note numbers are sent as they stand.

    bell 1..8: 65, 64, 62, 60, 58, 57, 55, 53
    channel 1 (adafruit out_channel 0)

Each stroke's NoteOn (a head trip here, a trigger press through note()) is
followed at once by one SysEx with the stroke's own keypad stamp
(midi_stamp.py; settings.json midi.stamp_sysex, default on).  A synth
ignores it; Sandbells ingest_midi times the stroke by it.

Does not sweep the orange threshold wire. Does not paint pixels: tripped()
feeds panel.panel_frame via concentrator.
"""

import time

NOTES = (65, 64, 62, 60, 58, 57, 55, 53)
VEL_ON = 100
VEL_OFF = 0
FULL = 65535
HEADS = ("head_new", "head_old")
# A re-trip on one head sooner than this is a double (bounce), not a stroke
# (54-125 ms doubles seen on the bench; a hand-swung beam gives >= 320 ms,
# a ringing bell >~1.5 s).  Integer ms: CircuitPython floats are 32-bit.
MIN_STROKE_GAP_MS = 250
NS_PER_MS = 1000000


def _ticks_ms():
    return time.monotonic_ns() // NS_PER_MS


def midi_port():
    """The usb_midi out port open_midi sends on (the stamps' port), or None."""
    try:
        import usb_midi
    except ImportError:
        return None
    ports = usb_midi.ports
    return ports[1] if len(ports) > 1 else (ports[0] if ports else None)


def open_midi():
    """USB MIDI out, or None (no port / library missing)."""
    try:
        import usb_midi
        import adafruit_midi
    except ImportError:
        print("USB MIDI: not available")
        return None
    out = midi_port()
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
    def __init__(self, inputs, lasers, midi=None, notes=NOTES, send_fn=None,
                 clock_ms=None, min_gap_ms=MIN_STROKE_GAP_MS, stamp=None):
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
        self.clock_ms = clock_ms or _ticks_ms   # int ms
        self.min_gap_ms = min_gap_ms
        self.on_at = [None] * self.n            # ms of the last counted stroke
        self.running = False
        # midi_stamp.MidiStamp (or None): its SysEx follows each stroke's NoteOn
        self.stamp = stamp

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
        print("%.3f RINGING" % time.monotonic(), [(c + 1, k, self.notes[c]) for c, k in sorted(self.heads.items())])

    def _send(self, ch, on, t_ms=None):
        """Note on/off.  An on is a stroke: its stamp SysEx goes right after
        the NoteOn (t_ms: the stroke's edge stamp; None = now)."""
        note = self.notes[ch]
        if self._send_fn is not None:
            self._send_fn(ch, note, on)
        elif self.midi is not None:
            self.midi.send(_note_msg(note, on))
        if on and self.stamp is not None:
            self.stamp.stroke(ch + 1, self.clock_ms() if t_ms is None else t_ms)
        print("%.3f ch %d %s %d%s" % (time.monotonic(), ch + 1, "ON" if on else "OFF", note,
                                       "" if (self.midi or self._send_fn) else " (no midi)"))

    def edge(self, ch, held, t_ms=None):
        """One sense edge on channel ch (held = beam state after it), at
        t_ms (int ms on clock_ms's scale: the edge's own keypad stamp,
        extended).  t_ms None = now."""
        if not self.running or ch not in self.heads or held == self.prev_held[ch]:
            return
        trip = held != self.rest_held[ch]
        if trip and not self.sounding[ch]:
            now = self.clock_ms() if t_ms is None else t_ms
            last = self.on_at[ch]
            if last is not None and now - last < self.min_gap_ms:
                self.prev_held[ch] = held      # double: no note, no stroke
                return
            self.on_at[ch] = now
            self.sounding[ch] = True
            self.strokes[ch] += 1
            self._send(ch, True, now)
        elif (not trip) and self.sounding[ch]:
            self.sounding[ch] = False
            self._send(ch, False)
        self.prev_held[ch] = held

    def tripped(self):
        return set(ch for ch in self.heads if self.sounding[ch])

    def note(self, ch, on, t_ms=None):
        """Send ch's bell note on or off outside the laser-head logic: a
        trigger plug press plays the same note its channel's bell would.
        t_ms: the press's edge stamp, for the NoteOn's stamp SysEx."""
        if 0 <= ch < self.n:
            self._send(ch, on, t_ms)

    def clock(self):
        """Sense loop, after the edges: a stamp clock message when none has
        gone for a while (midi_stamp.MidiStamp.clock).  Cheap when not due."""
        if self.stamp is not None:
            self.stamp.clock()

    def stop(self):
        """Notes off, lasers off (before a rescan or at exit)."""
        for ch in range(self.n):
            if self.sounding[ch]:
                self.sounding[ch] = False
                self._send(ch, False)
            self.lasers[ch].duty_cycle = 0
        self.running = False

    def status(self):
        st = {"running": self.running,
              "heads": dict((c + 1, k) for c, k in self.heads.items()),
              "strokes": list(self.strokes), "sounding": sorted(self.tripped())}
        if self.stamp is not None:
            st["stamp"] = self.stamp.status()
        return st


if __name__ == "__main__" or __name__ == "<module>":
    # Standalone: concentrator.py does the setup, then rings.
    import concentrator
    concentrator.run()
