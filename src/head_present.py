"""
head_present.py
===============
Is a laser head fitted on this channel?

A head is present only if pulsing its laser moves its own mux port.
The idle reading cannot answer this. With 100k on 4051 Z, an empty
port and a resting head both sit near 500–900 counts.

Confirmed 2026-10-01, Pico 2 W, GP26 = 4051 Z:
    ch 0 laser GP8  sense mux 0
    ch 1 laser GP9  sense mux 1
    ch 2 laser GP10 sense mux 2   not fitted
    ch 3 laser GP7  sense mux 3   not fitted
    address A GP19, B GP18, C GP17

Fitted heads step at about half duty and land near 9100 counts.
Empty ports do not. Threshold is 2000, clear of the empty-port rise
and well under the fitted step.

Does not read phototransistors, NeoPixels, Wi-Fi, or settings.json.
Does not decide that a bell happened.
"""

import time
import board
import pwmio
import analogio

LASERS = (8, 9, 10, 7)
MUX_OF = (0, 1, 2, 3)
ADDR = (board.GP19, board.GP18, board.GP17)  # A, B, C
SENSE = board.GP26
PULSE = 65535
SETTLE_S = 0.05
PRESENT_DELTA = 2000

_lasers = None
_addr = None
_adc = None

def _claim():
    global _lasers, _addr, _adc
    if _lasers is not None:
        return
    _lasers = []
    for gp in LASERS:
        _lasers.append(pwmio.PWMOut(getattr(board, "GP%d" % gp), frequency=1000, duty_cycle=0))
    _addr = []
    for pin in ADDR:
        p = digitalio_out(pin)
        _addr.append(p)
    _adc = analogio.AnalogIn(SENSE)

def digitalio_out(pin):
    import digitalio
    p = digitalio.DigitalInOut(pin)
    p.direction = digitalio.Direction.OUTPUT
    p.value = False
    return p

def select(port):
    _claim()
    for bit in range(3):
        _addr[bit].value = bool(port & (1 << bit))
    time.sleep(0.002)

def read(port):
    select(port)
    return _adc.value

def all_off():
    _claim()
    for p in _lasers:
        p.duty_cycle = 0

def probe(ch):
    """Return (present, dark, lit, delta) for one laser index."""
    _claim()
    port = MUX_OF[ch]
    all_off()
    time.sleep(SETTLE_S)
    dark = read(port)
    _lasers[ch].duty_cycle = PULSE
    time.sleep(SETTLE_S)
    lit = read(port)
    _lasers[ch].duty_cycle = 0
    delta = lit - dark
    return delta >= PRESENT_DELTA, dark, lit, delta

def fitted(channels=(0, 1, 2, 3)):
    """Pulse each channel. Return the indexes that respond."""
    found = []
    for ch in channels:
        ok, dark, lit, delta = probe(ch)
        print("ch %d mux %d dark %d lit %d d %+d %s" % (
            ch, MUX_OF[ch], dark, lit, delta, "HEAD" if ok else "empty"))
        if ok:
            found.append(ch)
    all_off()
    return found