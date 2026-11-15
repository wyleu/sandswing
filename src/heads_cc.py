"""
heads_cc.py -- the sandheads scope's readings and acks as MIDI Control
Change on the USB MIDI out (one-way, Pico -> host), for Sandbells'
ingest_midi.  Commands still come in on the USB data port (tune.SerialTune).
Map and reasons: docs/align-scope.md "MIDI CC map".

One report = a burst of CCs on CC_CHANNEL, written with ONE port write
from align_task (never from the sense path, so a stroke's NoteOn / stamp
SysEx is never split or reordered): CC_HEAD first (starts the report),
CC_SEQ last (ends it).  Values are 14-bit (MSB on CC n, LSB on CC n + 32,
the standard MIDI pairing for controllers 0-31); NONE_14 means "no
value".  14 bits because 7 (1/128 of full scale: 26 mV of PT level, 1.3
threshold steps) cannot show day / night drift or one nudge; the ADC is
12-bit, so 14 loses nothing.  Only sent while a host keeps the scope alive
(align_scope KEEPALIVE_MS), at most every SERIAL_FRAME_MS (500 ms; 1 s
while ringing, and then only the PT level: 8 messages, 32 USB bytes).

The buffer is preallocated: a report allocates one memoryview.
"""

CC_CHANNEL = 16                # 1-based; the bells' notes are on midi.usb.channel (1)
CC_STATUS = 0xB0               # Control Change, channel in the low nibble
CC_LSB = 32                    # 14-bit: LSB controller = MSB controller + this
# 7-bit controllers
CC_HEAD = 102                  # head (channel) 1-16: a report starts
CC_FLAGS = 103                 # FLAG_* bits
CC_ACK_WHAT = 104              # WHAT_* (an ack report)
CC_ACK_WHY = 105               # 0 ok, else WHY_* code
CC_SEQ = 106                   # report count mod 128: the report ends
# 14-bit controllers (MSB; LSB on +CC_LSB)
CC_THR = 20                    # threshold target duty, scaled (duty14)
CC_RB = 21                     # GP28 threshold readback, scaled (duty14)
CC_DRV = 22                    # laser drive duty, scaled (duty14)
CC_MA = 23                     # laser current, MA_UNITS per mA: the AVERAGE (above
                               # dark).  The on-current is CC_MA / (CC_DRV duty): no
                               # CC of its own (a full report is 15 of MAX_MSGS)
CC_PT = 24                     # PT level, mV
CC_PT_SPAN = 25                # PT spread over the burst, mV
NONE_14 = 16383                # "no value" (not measured / not wired)
MAX_14 = 16382
SEVEN = 127
DUTY_FULL = 65535              # a 16-bit duty; sent scaled to 0..MAX_14 (4-count steps)
MA_UNITS = 100                 # 0.01 mA (to 163.82 mA)
MV_PER_V = 1000
FLAG_CLEAR = 1                 # beam clear (comparator high) at the last sample
FLAG_BROKE = 2                 # a low sample since the last report
FLAG_BUSY = 4                  # mA is an older reading (tower not quiet / mux held)
FLAG_QUIET = 8                 # tower quiet: adjustments allowed
FLAG_SCOPE = 16                # the scope is running on this head
WHAT = {"thr": 1, "drive": 2, "scope": 3, "bad": 4, "thr_mid": 5}
# refusal text (tune.Tuner / align_scope) -> code, by prefix; 127: anything else
WHY = (("not an active head", 1), ("tower not quiet", 2), ("survey running", 3),
       ("old head has no threshold", 4), ("too fast", 5), ("at limit", 6),
       ("laser is off", 7), ("laser current over", 8), ("busy", 9), ("timeout", 10),
       ("head gone", 11), ("low memory", 12), ("bad", 13), ("unknown key", 13),
       ("nothing to do", 13), ("not an align command", 13), ("too long", 13))
WHY_OTHER = 127
MAX_MSGS = 16                  # CCs in one report (a full one is 15)


def why_code(why):
    if not why:
        return 0
    for prefix, code in WHY:
        if why.startswith(prefix):
            return code
    return WHY_OTHER


def duty14(d):
    """0..DUTY_FULL -> 0..MAX_14 (so full duty is not NONE_14); the host
    turns it back with value * DUTY_FULL / MAX_14, rounded."""
    return None if d is None else (int(d) * MAX_14 + DUTY_FULL // 2) // DUTY_FULL


def ma14(ma):
    return None if ma is None else int(ma * MA_UNITS + 0.5)


def mv14(v):
    return None if v is None else int(v * MV_PER_V + 0.5)


class CcOut:
    """Reports on a raw USB MIDI port (usb_midi.ports[1]).  Never raises."""

    def __init__(self, port, channel=CC_CHANNEL, ready=None):
        self.port = port
        self.ready = ready             # () -> bool: USB host there (MidiOut.ready)
        self.skipped = 0               # reports not sent: host gone
        self.short = 0                 # reports the TX FIFO took only part of
        self.status = CC_STATUS | ((channel - 1) & 0x0F)
        self.buf = bytearray(3 * MAX_MSGS)
        self.n = 0
        self.seq = 0
        self.reports = 0
        self.errors = 0

    def _cc(self, cc, v):
        i = self.n * 3
        b = self.buf
        b[i] = self.status
        b[i + 1] = cc
        b[i + 2] = v & SEVEN
        self.n += 1

    def _cc14(self, cc, v):
        if v is None:
            v = NONE_14
        elif v < 0:
            v = 0
        elif v > MAX_14:
            v = MAX_14
        self._cc(cc, v >> 7)
        self._cc(cc + CC_LSB, v & SEVEN)

    def _send(self):
        self.seq = (self.seq + 1) & SEVEN
        self._cc(CC_SEQ, self.seq)
        n = self.n * 3
        self.n = 0
        if self.ready is not None and not self.ready():
            self.skipped += 1
            return
        try:
            w = self.port.write(memoryview(self.buf)[:n])
            # PortOut.write never blocks: it takes whole messages while the
            # FIFO has room; the host's decoder drops a report without its SEQ
            if w is not None and w < n:
                self.short += 1
            else:
                self.reports += 1
        except Exception:
            self.errors += 1

    def frame(self, ch, flags, thr, rb, drv, ma, pt, span, full=True):
        """ch 1-based; thr/rb/drv duties, ma mA, pt/span volts (None: none).
        full False (ringing): head, flags and the PT level only."""
        self.n = 0
        self._cc(CC_HEAD, ch)
        self._cc(CC_FLAGS, flags)
        if full:
            self._cc14(CC_THR, duty14(thr))
            self._cc14(CC_RB, duty14(rb))
            self._cc14(CC_DRV, duty14(drv))
            self._cc14(CC_MA, ma14(ma))
        self._cc14(CC_PT, mv14(pt))
        self._cc14(CC_PT_SPAN, mv14(span))
        self._send()

    def ack(self, ch, what, why, thr, drv, flags):
        self.n = 0
        self._cc(CC_HEAD, ch or 0)
        self._cc(CC_FLAGS, flags)
        self._cc(CC_ACK_WHAT, WHAT.get(what, WHAT["bad"]))
        self._cc(CC_ACK_WHY, why_code(why))
        self._cc14(CC_THR, duty14(thr))
        self._cc14(CC_DRV, duty14(drv))
        self._send()


def open_cc(outputs, log=print):
    """CcOut on the USB MIDI output's raw port, or None (no USB MIDI)."""
    for out in outputs or ():
        if getattr(out, "name", None) == "usb" and getattr(out, "port", None) is not None:
            log("sandheads readings: MIDI CC on channel %d (USB MIDI)" % CC_CHANNEL)
            return CcOut(out.port, ready=getattr(out, "ready", None))
    return None
