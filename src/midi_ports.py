"""
midi_ports.py
=============
USB and 5-pin MIDI outputs, each with its own notes, channel and SysEx.

settings.json "midi" may be the old flat stamp pair (USB only):

    "midi": { "stamp_sysex": true, "clock_s": 10 }

or two similar port blocks:

    "midi": {
      "usb": {
        "enabled": true,
        "channel": 1,
        "notes": [65, 64, 62, 60, 58, 57, 55, 53],
        "stamp_sysex": true,
        "clock_s": 10
      },
      "five_pin": {
        "enabled": false,
        "tx_pin": 16,
        "channel": 1,
        "notes": [65, 64, 62, 60, 58, 57, 55, 53],
        "stamp_sysex": true,
        "clock_s": 10
      }
    }

channel is the MIDI channel 1-16. notes[i] is bell channel i+1.
five_pin.tx_pin is the GP number for DIN UART TX (31250 baud).
A flat block with no usb/five_pin keys still means USB only; five_pin
stays off so a board whose LED strip is still on GP16 is not driven
as UART.

Each stroke goes to 5-pin first, then USB: a USB host that has dropped
the link (the Pi 3's dwc_otg resets) cannot delay or skip the DIN note.
A send that raises is counted and logged (the first and every
OUT_ERR_LOG_EVERY-th), never passed on.  USB is skipped (counted) while
supervisor.runtime.usb_connected is False (tud_ready: not mounted or
suspended).  usb_midi PortOut.write does not block (CircuitPython 10.0.3:
tud_midi_stream_write takes what fits in the TX FIFO and returns; a host
that stops reading loses whole messages, not the ring).
"""

from midi_stamp import open_stamp_from_section


MIDI_BAUD = 31250                 # MIDI 1.0 DIN / UART rate
MIDI_CHANNEL_MIN = 1
MIDI_CHANNEL_MAX = 16
DEFAULT_NOTES = (65, 64, 62, 60, 58, 57, 55, 53)
DEFAULT_CHANNEL = 1
DEFAULT_TX_PIN = 16               # Pico 2 W UART0 TX; proven with midi-5pin_test

USB_DEFAULTS = {
    "enabled": True,
    "channel": DEFAULT_CHANNEL,
    "notes": list(DEFAULT_NOTES),
    "stamp_sysex": True,
    "clock_s": 10,
}
FIVE_PIN_DEFAULTS = {
    "enabled": False,
    "tx_pin": DEFAULT_TX_PIN,
    "channel": DEFAULT_CHANNEL,
    "notes": list(DEFAULT_NOTES),
    "stamp_sysex": True,
    "clock_s": 10,
}
SHARED_KEYS = ("enabled", "channel", "notes", "stamp_sysex", "clock_s")
OUT_ERR_LOG_EVERY = 100           # a port's send errors: logged on the 1st and every this many


def _midi_raw(cfg):
    if cfg is None:
        return {}
    sec = cfg.get("midi", {}) if hasattr(cfg, "get") else {}
    return sec if isinstance(sec, dict) else {}


def _channel(v):
    try:
        ch = int(v)
    except (TypeError, ValueError):
        ch = DEFAULT_CHANNEL
    if ch < MIDI_CHANNEL_MIN or ch > MIDI_CHANNEL_MAX:
        print("midi: channel %s ignored (want %d-%d); using %d" %
              (v, MIDI_CHANNEL_MIN, MIDI_CHANNEL_MAX, DEFAULT_CHANNEL))
        return DEFAULT_CHANNEL
    return ch


def _notes(v):
    if not v:
        return list(DEFAULT_NOTES)
    try:
        out = [int(x) for x in v]
    except (TypeError, ValueError):
        print("midi: notes %s ignored; using defaults" % (v,))
        return list(DEFAULT_NOTES)
    if not out:
        return list(DEFAULT_NOTES)
    return out


def _bool(v, default):
    return v if isinstance(v, bool) else default


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _apply(defaults, overlay):
    out = dict(defaults)
    if not isinstance(overlay, dict):
        return out
    if "enabled" in overlay:
        out["enabled"] = _bool(overlay["enabled"], defaults["enabled"])
    if "channel" in overlay:
        out["channel"] = _channel(overlay["channel"])
    if "notes" in overlay:
        out["notes"] = _notes(overlay["notes"])
    if "stamp_sysex" in overlay:
        out["stamp_sysex"] = _bool(overlay["stamp_sysex"], defaults["stamp_sysex"])
    if "clock_s" in overlay:
        out["clock_s"] = overlay["clock_s"]
    if "tx_pin" in overlay and "tx_pin" in defaults:
        out["tx_pin"] = _int(overlay["tx_pin"], defaults["tx_pin"])
    return out


def parse_ports(cfg):
    """(usb_spec, five_pin_spec) dicts, defaults filled in."""
    sec = _midi_raw(cfg)
    nested = isinstance(sec.get("usb"), dict) or isinstance(sec.get("five_pin"), dict)
    if nested:
        usb = _apply(USB_DEFAULTS, sec.get("usb"))
        five = _apply(FIVE_PIN_DEFAULTS, sec.get("five_pin"))
        return usb, five
    flat = {k: sec[k] for k in SHARED_KEYS if k in sec}
    return _apply(USB_DEFAULTS, flat), _apply(FIVE_PIN_DEFAULTS, {})


def _note_msg(note, on):
    from adafruit_midi.note_on import NoteOn
    from adafruit_midi.note_off import NoteOff
    from ring import VEL_ON, VEL_OFF
    return NoteOn(note, VEL_ON) if on else NoteOff(note, VEL_OFF)


class MidiOut:
    """One destination: adafruit MIDI port + optional MidiStamp + per-port notes."""

    def __init__(self, name, midi, notes, stamp=None, port=None, ready=None, log=print):
        self.name = name
        self.port = port                 # the raw port (heads_cc writes CC reports to it)
        self.midi = midi
        self.notes = list(notes)
        self.stamp = stamp
        self.ready = ready               # () -> bool; False: host gone, skip (USB only)
        self.log = log
        self.errors = 0                  # sends that raised (not passed on)
        self.skipped = 0                 # sends skipped while not ready

    def _err(self, what, e):
        self.errors += 1
        if self.errors % OUT_ERR_LOG_EVERY == 1:
            try:
                self.log("midi %s: %s failed (%d so far): %r" % (self.name, what, self.errors, e))
            except Exception:
                pass

    def send(self, ch, on, t_ms):
        """-> True if the note went.  Never raises."""
        if self.midi is None or ch < 0 or ch >= len(self.notes):
            return False
        if self.ready is not None and not self.ready():
            self.skipped += 1
            return False
        try:
            self.midi.send(_note_msg(self.notes[ch], on))
        except Exception as e:
            self._err("note", e)
            return False
        if on and self.stamp is not None:
            try:
                self.stamp.stroke(ch + 1, t_ms)
            except Exception as e:       # MidiStamp guards its write; this is belt and braces
                self._err("stamp", e)
        return True

    def clock(self):
        if self.stamp is None or (self.ready is not None and not self.ready()):
            return
        try:
            self.stamp.clock()
        except Exception as e:
            self._err("clock", e)

    def status(self):
        st = self.stamp.status() if self.stamp is not None else {}
        st["errors_out"] = self.errors
        st["skipped"] = self.skipped
        return st


def usb_ready():
    """() -> bool: supervisor.runtime.usb_connected (no allocation: a const
    bool), or None when there is no supervisor (host tests)."""
    try:
        import supervisor
        rt = supervisor.runtime
    except (ImportError, AttributeError):
        return None
    return lambda: rt.usb_connected


def open_usb_midi(channel):
    """(adafruit MIDI, raw write port) or (None, None)."""
    try:
        import usb_midi
        import adafruit_midi
    except ImportError:
        return None, None
    ports = usb_midi.ports
    out = ports[1] if len(ports) > 1 else (ports[0] if ports else None)
    if out is None:
        print("USB MIDI: no port")
        return None, None
    print("USB MIDI ch %d" % channel)
    return adafruit_midi.MIDI(midi_out=out, out_channel=channel - 1), out


def open_five_pin_midi(tx_pin, channel):
    """(adafruit MIDI, UART) or (None, None). GP16 is UART0 TX on Pico 2 W."""
    try:
        import board
        import busio
        import adafruit_midi
    except ImportError:
        print("five_pin MIDI: UART not available")
        return None, None
    pin = getattr(board, "GP%d" % tx_pin, None)
    if pin is None:
        print("five_pin MIDI: no GP%d" % tx_pin)
        return None, None
    try:
        uart = busio.UART(tx=pin, rx=None, baudrate=MIDI_BAUD, timeout=0.001)
    except (ValueError, TypeError) as e:
        print("five_pin MIDI: GP%d not opened (%s)" % (tx_pin, e))
        return None, None
    print("five_pin MIDI GP%d ch %d" % (tx_pin, channel))
    return adafruit_midi.MIDI(midi_out=uart, out_channel=channel - 1), uart


def open_outputs(cfg, clock_ms, neopixel_gp=None, log=print, ready=None):
    """MidiOut list for every enabled port that opened.  Order: 5-pin, then
    USB (the order every stroke is sent in).  ready: USB's ready test
    (default usb_ready())."""
    usb_spec, five_spec = parse_ports(cfg)
    outs = []
    if five_spec["enabled"]:
        tx_pin = five_spec["tx_pin"]
        if neopixel_gp is not None and int(tx_pin) == int(neopixel_gp):
            print("five_pin MIDI: tx_pin GP%d is pins.neopixel; not opening" % tx_pin)
        else:
            midi, port = open_five_pin_midi(tx_pin, five_spec["channel"])
            if midi is not None:
                stamp = open_stamp_from_section(five_spec, port, clock_ms, name="five_pin")
                outs.append(MidiOut("five_pin", midi, five_spec["notes"], stamp, log=log))
    if usb_spec["enabled"]:
        midi, port = open_usb_midi(usb_spec["channel"])
        if midi is not None:
            stamp = open_stamp_from_section(usb_spec, port, clock_ms, name="usb")
            outs.append(MidiOut("usb", midi, usb_spec["notes"], stamp, port=port,
                                ready=ready if ready is not None else usb_ready(), log=log))
    return outs
