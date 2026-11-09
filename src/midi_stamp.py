"""
midi_stamp.py
=============
The Pico's own time for each stroke, on the USB MIDI stream.

USB MIDI carries no time, and the host's arrival stamp jitters by tens of
ms (sandbells2, 2026-11-03: gaps 3170-3244 ms for a ±3 ms ring by the
keypad stamps).  So right after each stroke's NoteOn (ring.Ring._send: a
laser-head stroke or a trigger-plug press) one SysEx goes out with the
stroke's keypad edge stamp.  A synth ignores it (MIDI 1.0: a receiver
drops SysEx with a manufacturer ID it does not know; 0x7D is the
non-commercial ID, never a product's).  Sandbells ingest_midi
(events/midiparse.py, midiingest.py) pairs it with the NoteOn.

One message, 13 bytes, every byte after F0 is 7-bit:

    F0 7D 53 vv sh sl cc t4 t3 t2 t1 t0 F7

    7D     STAMP_MFR_ID   non-commercial / educational manufacturer ID
    53     STAMP_TAG      'S' (sandswing)
    vv     STAMP_VERSION  layout version, 1 = this table
    sh sl  seq, 14 bits, high 7 then low 7.  Stroke stamps count 1, 2, ..
           16383, then 1 again (0 is never a stroke's: "none yet").
    cc     bell channel 1-8 (Ring's ch + 1, the body "ch" of a stroke; NOT
           the MIDI channel, which stays 1).  0 = a clock message (below).
    t4..t0 the stamp, ms on the Pico's tick scale, 35 bits as 5 x 7 bits,
           most significant first: t = t4<<28 | t3<<21 | t2<<14 | t1<<7 | t0.
           The scale is sntp_pico.TickClock (supervisor.ticks_ms extended
           past its 2**29 wrap), the same ms the stroke's raw_tick and the
           ring's double-stroke gap use.  It is a free-running ms count,
           not 0 at boot (CircuitPython starts ticks_ms just under 2**29),
           so only differences and the host's offset mean anything.
           35 bits = about a year of uptime before the host sees a wrap.

Clock message (cc = 0): the current ms (TickClock.now(), not an edge),
seq = the last stroke stamp's seq (0 before the first).  Sent from the
sense loop when no stamp has gone for midi.clock_s (default 10 s), so the
host's offset estimate stays fresh between rings and it can tell a lost
stroke stamp (seq moved) while the tower is quiet.  Never while strokes
are going out (each stroke stamp restarts the wait).

Settings (settings.json "midi", both optional, defaults here):
    stamp_sysex  true   false: no SysEx at all (NoteOn/Off as before)
    clock_s      10     0: no clock messages (stroke stamps only)

Cost in the ring loop: one preallocated 13-byte bytearray, filled in place
(small-int arithmetic; a long int only past ~6 days of uptime) and one
port write, after the NoteOn.  A write that raises is counted, never
raised: the note has already gone.
"""

SYSEX_START = 0xF0
SYSEX_END = 0xF7
STAMP_MFR_ID = 0x7D          # MIDI non-commercial ID: no product uses it
STAMP_TAG = 0x53             # 'S': sandswing
STAMP_VERSION = 1
STAMP_CH_CLOCK = 0           # cc of a clock message
SEVEN_BITS = 7
DATA_MASK = 0x7F             # a MIDI data byte
STAMP_SEQ_BYTES = 2
STAMP_SEQ_MAX = (1 << (SEVEN_BITS * STAMP_SEQ_BYTES)) - 1     # 16383, then 1
STAMP_T_BYTES = 5
STAMP_T_BITS = SEVEN_BITS * STAMP_T_BYTES                     # 35
STAMP_T_MASK = (1 << STAMP_T_BITS) - 1

# byte offsets in the message
I_START, I_MFR, I_TAG, I_VER, I_SEQ, I_CH = 0, 1, 2, 3, 4, 6
I_T = I_CH + 1
I_END = I_T + STAMP_T_BYTES
STAMP_LEN = I_END + 1                                         # 13

STAMP_SYSEX_DEFAULT = True
CLOCK_S_DEFAULT = 10
MS_PER_S = 1000


def settings_from_section(sec):
    """(stamp_sysex, clock_ms) from one port dict (usb or five_pin)."""
    sec = sec if isinstance(sec, dict) else {}
    on = sec.get("stamp_sysex", STAMP_SYSEX_DEFAULT)
    on = on if isinstance(on, bool) else STAMP_SYSEX_DEFAULT
    try:
        clock_ms = max(0, int(float(sec.get("clock_s", CLOCK_S_DEFAULT)) * MS_PER_S))
    except (TypeError, ValueError):
        clock_ms = CLOCK_S_DEFAULT * MS_PER_S
    return on, clock_ms


def settings(cfg):
    """(stamp_sysex, clock_ms) from cfg "midi", else the defaults.
    A nested midi.usb block wins over a flat stamp pair.
    cfg: a config_loader.Config, a plain dict or None."""
    sec = cfg.get("midi", {}) if cfg is not None else {}
    sec = sec if isinstance(sec, dict) else {}
    if isinstance(sec.get("usb"), dict):
        inherit = {}
        if "stamp_sysex" in sec:
            inherit["stamp_sysex"] = sec["stamp_sysex"]
        if "clock_s" in sec:
            inherit["clock_s"] = sec["clock_s"]
        inherit.update(sec["usb"])
        return settings_from_section(inherit)
    return settings_from_section(sec)


def fill(buf, seq, ch, t_ms):
    """Write one message into buf (STAMP_LEN bytes, header preset) in place."""
    buf[I_SEQ] = (seq >> SEVEN_BITS) & DATA_MASK
    buf[I_SEQ + 1] = seq & DATA_MASK
    buf[I_CH] = ch & DATA_MASK
    t = int(t_ms) & STAMP_T_MASK
    i = I_END - 1
    while i >= I_T:
        buf[i] = t & DATA_MASK
        t >>= SEVEN_BITS
        i -= 1
    return buf


def new_buffer():
    buf = bytearray(STAMP_LEN)
    buf[I_START] = SYSEX_START
    buf[I_MFR] = STAMP_MFR_ID
    buf[I_TAG] = STAMP_TAG
    buf[I_VER] = STAMP_VERSION
    buf[I_END] = SYSEX_END
    return buf


def decode(msg):
    """(seq, ch, t_ms) of one message, or None (not ours / not this
    version).  For tests and tools; the host has its own (Sandbells)."""
    if (len(msg) != STAMP_LEN or msg[I_START] != SYSEX_START or msg[I_END] != SYSEX_END
            or msg[I_MFR] != STAMP_MFR_ID or msg[I_TAG] != STAMP_TAG
            or msg[I_VER] != STAMP_VERSION):
        return None
    t = 0
    for i in range(I_T, I_END):
        t = (t << SEVEN_BITS) | msg[i]
    return (msg[I_SEQ] << SEVEN_BITS) | msg[I_SEQ + 1], msg[I_CH], t


class MidiStamp:
    """Writes stroke and clock stamps to a USB MIDI out port (anything with
    write(buf)).  clock_ms: the TickClock.now the stroke stamps are on."""

    def __init__(self, port, clock_ms, clock_period_ms=CLOCK_S_DEFAULT * MS_PER_S):
        self.port = port
        self.clock_ms = clock_ms
        self.clock_period_ms = clock_period_ms
        self.buf = new_buffer()
        self.seq = 0                 # last stroke stamp's seq (0: none yet)
        self.sent = 0                # stroke stamps written
        self.clocks = 0              # clock messages written
        self.errors = 0              # writes that raised
        self.last_ms = None          # tick ms of the last write (stroke or clock)

    def stroke(self, ch, t_ms):
        """ch: bell channel 1-8.  t_ms: the stroke's edge stamp (tick ms)."""
        self.seq = self.seq % STAMP_SEQ_MAX + 1
        if self._write(self.seq, ch, t_ms):
            self.sent += 1

    def clock(self, now_ms=None):
        """A clock message if none has gone for clock_period_ms.  -> sent."""
        if not self.clock_period_ms:
            return False
        now = self.clock_ms() if now_ms is None else now_ms
        if self.last_ms is not None and now - self.last_ms < self.clock_period_ms:
            return False
        if self._write(self.seq, STAMP_CH_CLOCK, now):
            self.clocks += 1
        self.last_ms = now          # even after an error: no retry every pass
        return True

    def _write(self, seq, ch, t_ms):
        fill(self.buf, seq, ch, t_ms)
        try:
            self.port.write(self.buf)
        except Exception:            # the note went first; never stop the ring
            self.errors += 1
            return False
        self.last_ms = self.clock_ms() if ch != STAMP_CH_CLOCK else t_ms
        return True

    def status(self):
        return {"seq": self.seq, "sent": self.sent, "clocks": self.clocks,
                "errors": self.errors}


def open_stamp_from_section(sec, port, clock_ms, name="usb"):
    """MidiStamp on port per one port dict, or None (off, or no port)."""
    on, clock_period_ms = settings_from_section(sec)
    if not on or port is None:
        return None
    label = name + " MIDI stamps"
    print("%s: SysEx after each NoteOn, clock every %d ms" % (label, clock_period_ms)
          if clock_period_ms else "%s: SysEx after each NoteOn, no clock" % label)
    return MidiStamp(port, clock_ms, clock_period_ms)


def open_stamp(cfg, port, clock_ms):
    """MidiStamp on port per cfg "midi", or None (off, or no port)."""
    on, clock_period_ms = settings(cfg)
    if not on or port is None:
        return None
    print("USB MIDI stamps: SysEx after each NoteOn, clock every %d ms" % clock_period_ms
          if clock_period_ms else "USB MIDI stamps: SysEx after each NoteOn, no clock")
    return MidiStamp(port, clock_ms, clock_period_ms)
