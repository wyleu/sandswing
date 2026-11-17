"""
head_store.py -- line-up values per channel in the Pico's NVM
(microcontroller.nvm): laser drive, threshold and the number written on
the head.  The line-up: a new head goes in channel 1 (the one with the PT
wire), the sandheads page sweeps it and suggests a drive and threshold,
and "Save for channel N" stores them for the channel the head will go to.
At boot a saved value replaces the default drive (tune.LASER_MAX_DUTY) and
the seeded threshold (threshold.default_duty); the survey still runs and
still decides which channels are heads.  Heads carry no ID, so a record
belongs to a channel, not to a head: the label only reminds a person.

Written only by an explicit page command (align "save" / "forget"), only
while the tower is quiet (an NVM write is a flash erase + program: the
chip stalls for it), never from the stroke path.  One slice write.

Layout at NVM_OFFSET (NVM_USED_B of the reserved NVM_SIZE bytes):
  0  MAGIC (2)  2  NVM_VERSION (1)  3  SLOTS (1)
  4  SLOTS records of REC_B: flags (1: F_SAVED, F_THR), drive u16 LE,
     threshold u16 LE, label LABEL_MAX bytes (ASCII, NUL padded).  No
     F_SAVED: no record; no F_THR: no threshold (an old head has none)
  .. CRC-16/CCITT (init CRC_INIT) of everything before it, u16 LE
A wrong magic, version, slot count or CRC: every slot is empty (ignored,
"state" says why); the next save writes a fresh image.

The last WATCHDOG_MARK_B bytes of the NVM are watchdog_guard's reset note;
nothing else in sandswing uses the NVM (tests/test_head_store.py checks).
"""

NVM_OFFSET = 0
NVM_SIZE = 128                  # reserved for this store from NVM_OFFSET
WATCHDOG_MARK_B = 4             # watchdog_guard.mark_reset: the NVM's last 4 bytes
MAGIC = b"SH"
NVM_VERSION = 1
SLOTS = 8                       # channels 1..8
LABEL_MAX = 8                   # characters of the head's number
# allowed in a label: a person types the number written on the head
LABEL_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_./# "
F_SAVED = 1
F_THR = 2
DUTY_MAX = 0xFFFF
FLAG_B = 1
U16_B = 2
BYTE = 0xFF
HDR_B = 4
REC_B = FLAG_B + U16_B + U16_B + LABEL_MAX
LABEL_AT = FLAG_B + 2 * U16_B   # within a record
CRC_B = 2
NVM_USED_B = HDR_B + SLOTS * REC_B + CRC_B
CRC_INIT = 0xFFFF
CRC_POLY = 0x1021
CRC_TOP = 0x8000
CRC_MASK = 0xFFFF
BITS = 8


def crc16(data, n=None):
    c = CRC_INIT
    for i in range(len(data) if n is None else n):
        c ^= data[i] << BITS
        for _ in range(BITS):
            c = ((c << 1) ^ CRC_POLY) & CRC_MASK if c & CRC_TOP else (c << 1) & CRC_MASK
    return c


def check_label(s):
    """-> the label (str, may be "").  ValueError("bad label") if too long
    or a character outside LABEL_CHARS."""
    if s is None:
        return ""
    if not isinstance(s, str) or len(s) > LABEL_MAX:
        raise ValueError("bad label")
    for ch in s:
        if ch not in LABEL_CHARS:
            raise ValueError("bad label")
    return s.strip()


def _u16(b, i):
    return b[i] | (b[i + 1] << BITS)


def _put16(b, i, v):
    b[i] = v & BYTE
    b[i + 1] = (v >> BITS) & BYTE


class HeadStore:
    """slots[i] (channel i + 1): None or (drive, thr or None, label)."""

    def __init__(self, nvm):
        self.nvm = nvm
        self.slots = [None] * SLOTS
        self.state = "none"         # ok / empty / bad ... / none (no NVM)
        self.writes = 0
        if nvm is None:
            return
        if NVM_OFFSET + NVM_SIZE > len(nvm) - WATCHDOG_MARK_B or NVM_USED_B > NVM_SIZE:
            self.nvm = None
            self.state = "nvm too small"
            return
        self.load()

    def load(self):
        try:
            b = bytes(self.nvm[NVM_OFFSET:NVM_OFFSET + NVM_USED_B])
        except Exception:
            self.state = "unreadable"
            return
        self.slots = [None] * SLOTS
        if b[0:2] != MAGIC:
            self.state = "empty"
            return
        if b[2] != NVM_VERSION or b[3] != SLOTS:
            self.state = "bad version"
            return
        end = NVM_USED_B - CRC_B
        if crc16(b, end) != _u16(b, end):
            self.state = "bad checksum"
            return
        for i in range(SLOTS):
            o = HDR_B + i * REC_B
            f = b[o]
            if not f & F_SAVED:
                continue
            drv = _u16(b, o + FLAG_B)
            thr = _u16(b, o + FLAG_B + U16_B)
            lab = b[o + LABEL_AT:o + REC_B]
            k = lab.find(b"\0")
            lab = lab if k < 0 else lab[:k]
            self.slots[i] = (drv, thr if f & F_THR else None, lab.decode())
        self.state = "ok"

    def image(self, slots):
        b = bytearray(NVM_USED_B)
        b[0] = MAGIC[0]
        b[1] = MAGIC[1]
        b[2] = NVM_VERSION
        b[3] = SLOTS
        for i in range(SLOTS):
            o = HDR_B + i * REC_B
            s = slots[i]
            if s is None:
                continue
            b[o] = F_SAVED | (0 if s[1] is None else F_THR)
            _put16(b, o + FLAG_B, s[0])
            _put16(b, o + FLAG_B + U16_B, 0 if s[1] is None else s[1])
            lab = s[2].encode()
            for j in range(len(lab)):
                b[o + LABEL_AT + j] = lab[j]
        end = NVM_USED_B - CRC_B
        _put16(b, end, crc16(b, end))
        return b

    def _write(self, slots):
        """One slice write, then read back.  -> None or why."""
        if self.nvm is None:
            return "not saved: no NVM"
        img = self.image(slots)
        self.nvm[NVM_OFFSET:NVM_OFFSET + NVM_USED_B] = img
        self.writes += 1
        self.load()
        if self.state != "ok" or self.slots != slots:
            return "not saved: read back differs"
        return None

    def save(self, slot, drive, thr, label):
        """slot 0-based.  drive / thr duties (thr None: none).  -> None or why."""
        if not 0 <= slot < SLOTS:
            return "bad channel"
        if drive is None or not 0 < drive <= DUTY_MAX:
            return "laser is off"
        if thr is not None and not 0 <= thr <= DUTY_MAX:
            thr = None
        new = list(self.slots)
        new[slot] = (int(drive), None if thr is None else int(thr), check_label(label))
        return self._write(new)

    def forget(self, slot):
        if not 0 <= slot < SLOTS:
            return "bad channel"
        new = list(self.slots)
        new[slot] = None
        return self._write(new)

    def drive_map(self, n):
        """{ch0: drive} of the saved channels below n."""
        return dict((i, s[0]) for i, s in enumerate(self.slots) if s is not None and i < n)

    def thr_map(self, n):
        return dict((i, s[1]) for i, s in enumerate(self.slots)
                    if s is not None and s[1] is not None and i < n)

    def brief(self):
        return {"state": self.state, "saved": [i + 1 for i, s in enumerate(self.slots) if s]}


def open_store(log=print):
    try:
        import microcontroller
        nvm = microcontroller.nvm
    except (ImportError, AttributeError):
        nvm = None
    try:
        st = HeadStore(nvm)
    except Exception as e:
        log("head store off: %r" % e)
        return None
    log("head store (NVM): %s, saved %s" % (st.state, st.brief()["saved"]))
    return st
