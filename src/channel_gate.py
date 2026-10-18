# channel_gate.py -- never drive a channel whose socket is not known. Leaf.
DRIVE_OK = ("loopback", "head_new", "head_old")


class ChannelGate:
    """Per-channel permission for laser PWM and threshold drive.
    unknown/empty/fault -> drive must stay at 0 (except the classifier's own
    short, low-energy probes).  A fault is latched until clear()."""

    def __init__(self, n):
        self.kind = ["unknown"] * n
        self.latched = [False] * n

    def set_kind(self, ch, kind):
        if self.latched[ch] and kind != "fault":
            return self.kind[ch]           # stays fault until cleared
        self.kind[ch] = kind
        if kind == "fault":
            self.latched[ch] = True
        return kind

    def clear(self, ch):
        self.latched[ch] = False
        self.kind[ch] = "unknown"

    def may_drive(self, ch):
        return self.kind[ch] in DRIVE_OK

    def enforce(self, bells):
        """Call every loop / heartbeat: force drive off where not allowed."""
        for b in bells:
            if not self.may_drive(b.index):
                if getattr(b, "laser", None) is not None:
                    b.laser.duty_cycle = 0

    def status(self):
        return {"kind": list(self.kind),
                "latched": [i for i, v in enumerate(self.latched) if v]}
