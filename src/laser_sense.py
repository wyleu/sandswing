"""
laser_sense.py
==============
CircuitPython bench check: which 4051 port follows which laser drive,
then the duty/current curve for each head that is actually fitted.

Raspberry Pi Pico 2 W. Not a tower program. Not launched in normal use.

WHY THIS EXISTS
    The 4051 Y pins are not in channel order round the package, and the
    address pins are easy to point at the wrong GPIO. Detection, lineup
    and any current-sense trim are meaningless until one mux port is
    shown to move with one laser and the others are not.
    Which heads are plugged in changes. This file does not keep a list.

WHAT IT DOES
    1. Claims every laser PWM in LASERS, and the 4051.
    2. Tries Wi-Fi and the farm WebSocket. Failure is printed and ignored.
       settings.json must have the Wi-Fi block. output.ws must be enabled
       or start_server prints "output.ws disabled" and the curve stays
       on serial only.
    3. All lasers off. Holds HOLD_S so a meter can settle.
       Reads all 8 mux ports. That is the dark line.
    4. Pulses every laser in LASERS at duty 65535, one at a time.
       A head is fitted if its best mux port rises by more than PRESENT_DELTA.
       Neighbour ports rise by about 3000 from coupling. The real port
       rises by about 10000. 6000 sits between those two.
    5. Stores that set in microcontroller.nvm.
       Byte 0 is 0xA5. Byte 1 is a bit mask, bit 0 = LASERS[0].
    6. Ramps each fitted channel on the mux port that followed it.
       Each duty is sampled PEAK_N times and the maximum kept, so a
       1 kHz PWM is not reported from the gap between pulses.
       Each step is printed and sent as a dict:
           {"kind":"laser_curve","ch","gp","mux","duty","count"}
       farm_ws.send json-encodes that dict. A done record closes the channel.
    7. Turns the lasers off and keeps polling the socket.

WHAT IT DOES NOT DO
    - Does not read the phototransistors. Unplug those outputs before
      running, or a head can masquerade as laser current.
    - Does not use the 393, NeoPixels, or MIDI.
    - Does not write CIRCUITPY or settings.json.
    - Does not decide that a bell happened.
    - Does not launch itself. From the REPL:
          exec(open("laser_sense.py").read())

PINS (this board, after the drive swap was undone)
    Laser PWM, 1 kHz. Index is the channel.
        ch 0 GP8     ch 1 GP9     ch 2 GP10    ch 3 GP11
    Measured map, three heads fitted:
        GP8 -> Y0    GP9 -> Y1    GP10 -> Y2    GP11 empty
    4051 address, A = least significant bit:
        A GP19    B GP18    C GP17
    4051 Z into the laser-current ADC, 100k pull-down:
        GP26
    GP28 is the phototransistor ADC. Do not point SENSE_ADC at it.
"""

import time
import board
import pwmio
import analogio
import digitalio
import microcontroller
from config_loader import load_config
from farm_ws import connect_wifi, start_server, poll, send

HOLD_S = 5.0
STEP_S = 0.4
PRESENT_DELTA = 6000
PEAK_N = 40
LASERS = (8, 9, 10, 11)          # index = channel; append a GP for a new head
MUX_PINS = (board.GP19, board.GP18, board.GP17)  # A, B, C
SENSE_ADC = board.GP26          # 4051 Z
MUX_PORTS = 8
DUTIES = (0, 2048, 4096, 8192, 16384, 24576, 32768, 40960, 49152, 57344, 65535)
NVM_MAGIC = 0xA5
PEAK_MS = 15


addr = []
for p in MUX_PINS:
    d = digitalio.DigitalInOut(p)
    d.direction = digitalio.Direction.OUTPUT
    d.value = False
    addr.append(d)

lasers = []
for gp in LASERS:
    lasers.append(pwmio.PWMOut(getattr(board, "GP%d" % gp), frequency=1000, duty_cycle=0))

cfg = load_config()
try:
    connect_wifi(cfg)
    start_server(cfg, lambda: {"kind": "laser_sense"})
except Exception as e:
    print("ws not up:", e)

adc = analogio.AnalogIn(SENSE_ADC)

def mux(port):
    for bit in range(3):
        addr[bit].value = bool(port & (1 << bit))
    time.sleep(0.02)


def read(port):
    mux(port)
    return adc.value


def peak(port):
    mux(port)
    best = 0
    end = time.monotonic() + (PEAK_MS / 1000.0)
    while time.monotonic() < end:
        v = adc.value
        if v > best:
            best = v
    return best


def all_off():
    for p in lasers:
        p.duty_cycle = 0


def snap():
    return [peak(i) for i in range(MUX_PORTS)]


def service(seconds):
    t = time.monotonic() + seconds
    while time.monotonic() < t:
        try:
            poll()
        except Exception:
            pass
        time.sleep(0.05)


def emit(obj):
    print(obj)
    try:
        send(obj)
    except Exception:
        pass


def store_fitted(found):
    mask = 0
    for ch in found:
        mask |= 1 << ch
    microcontroller.nvm[0] = NVM_MAGIC
    microcontroller.nvm[1] = mask & 0xFF
    print("nvm fitted %s mask %d" % (sorted(found), mask))


def identify(dark):
    """Pulse every drive. Return {channel: mux port} for heads that moved."""
    found = {}
    for ch in range(len(LASERS)):
        all_off()
        lasers[ch].duty_cycle = 65535
        print("ch %d GP%d ON" % (ch, LASERS[ch]))
        service(HOLD_S)
        lit = snap()
        print("lit ", lit)
        best, best_d = None, 0
        for port in range(MUX_PORTS):
            d = lit[port] - dark[port]
            flag = "FOLLOWS" if d > PRESENT_DELTA else "still"
            print("  mux %d  dark %5d  lit %5d  d %+6d  %s" % (
                port, dark[port], lit[port], d, flag))
            if d > best_d:
                best, best_d = port, d
        if best is not None and best_d > PRESENT_DELTA:
            found[ch] = best
            print("ch %d fitted, mux %d" % (ch, best))
        else:
            print("ch %d empty" % ch)
        lasers[ch].duty_cycle = 0
        service(HOLD_S)
    return found


def ramp(ch, port):
    print("RAMP ch %d GP%d via mux %d" % (ch, LASERS[ch], port))
    prev = None
    for duty in DUTIES:
        all_off()
        lasers[ch].duty_cycle = duty
        service(STEP_S)
        count = peak(port)
        emit({
            "kind": "laser_curve",
            "ch": ch,
            "gp": LASERS[ch],
            "mux": port,
            "duty": duty,
            "count": count,
        })
        if prev is not None and duty:
            dv = count - prev
            if dv < 400:
                print("  flat")
            elif prev < 5000 and dv > PRESENT_DELTA:
                print("  rising")
        prev = count
    lasers[ch].duty_cycle = 0
    emit({"kind": "laser_curve_done", "ch": ch, "points": len(DUTIES)})


print("laser_sense lasers %s" % (LASERS,))
print("PT outputs should be unplugged")
all_off()
print("all off")

service(HOLD_S)
adc.deinit()
adc = analogio.AnalogIn(SENSE_ADC)
print("adc", adc.value)
dark = snap()
print("dark", dark)

found = identify(dark)
store_fitted(found)
print("fitted", sorted(found))

for ch in sorted(found):
    ramp(ch, found[ch])

all_off()
print("done")
while True:
    service(1)
