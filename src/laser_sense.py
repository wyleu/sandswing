"""
laser_sense.py
==============
CircuitPython bench check: which 4051 port follows which laser drive.
Raspberry Pi Pico 2 W. Not a tower program. Not launched in normal use.

WHY THIS EXISTS
    The 4051 Y pins are not in channel order round the package, and the
    address pins are easy to point at the wrong GPIO. Detection, lineup
    and any current-sense trim are meaningless until one mux port is
    shown to move with one laser and the others are not.
    Run once after wiring. Run again only if a connector or a GPIO
    assignment moves. Leave startup.program pointing at sandswing.py
    the rest of the time.

WHAT IT DOES
    1. Claims the laser PWMs and the 4051. Claims nothing else.
    2. All lasers off. Holds HOLD_S (5 s) so a meter can settle.
       Reads all 8 mux ports. That is the dark line.
    3. For each index in ACTIVE: that laser at duty 65535, others off,
       hold 5 s, read all 8 ports again.
    4. Prints dark, lit, and lit-dark for every port.
       FOLLOWS means the delta beat 2000 counts. The port with the
       largest positive delta is reported as the sense input for that
       laser. A smaller rise on a neighbour is a floating Y pin, not
       a second copy of the laser.
    5. Turns the lasers off and sleeps. It does not reboot.

WHAT IT DOES NOT DO
    - Does not read the phototransistors. Unplug those outputs before
      running, or a head can masquerade as laser current.
    - Does not use the 393, NeoPixels, Wi-Fi, MIDI, or nvm.
    - Does not write CIRCUITPY, settings.json, or a calibration.
    - Does not decide that a bell happened.
    - Does not launch itself. code.py only reaches it if
      startup.program is "laser_sense.py". A broken settings.json
      makes code.py die first; from the REPL:
          exec(open("laser_sense.py").read())

PINS (this board, confirmed 2026-10-01)
    Laser PWM, 1 kHz, duty 0 or 65535:
        ch 0 GP8     ch 1 GP9     ch 2 GP10    ch 3 GP7
    Only ACTIVE = (0, 1) are driven. ch 2 and ch 3 are not fitted.
    4051 address, A = least significant bit:
        A GP19    B GP18    C GP17
    4051 Z (common) into the laser-current ADC:
        GP26
    GP28 is the phototransistor ADC. Do not point SENSE_ADC at it.
    An earlier run on GP11/12/13 and GP28 read the same voltage on
    every "port" because those pins are not the mux.

RESULT OF THE GOOD RUN
    dark ports differed (mux was switching).
    GP8 on: mux 0 moved +11475. Neighbours moved less, floating Y.
    GP9 on: mux 1 moved +11235. Same pattern.
    Map to keep:
        laser GP8  channel 0  sense mux 0
        laser GP9  channel 1  sense mux 1
    Mux 2..7 are unconnected. Do not use them in detection.

HOW TO READ A LATER RUN
    Eight similar dark numbers: address pins or Z are wrong again.
    One port jumps by thousands, the rest stay: that port is the map.
    Every port jumps together: Z is shared and the address is not
    changing, or /INH (4051 pin 6) is high or floating.
"""
import time
import board
import pwmio
import analogio
import digitalio

HOLD_S = 5.0
LASERS = (8, 9, 10, 7)          # ch 0..3, change to match settings.json
MUX_PINS = (board.GP19, board.GP18, board.GP17)  # A, B, C
SENSE_ADC = board.GP26          # 4051 Z
ACTIVE = (0, 1)                 # heads actually wired

adc = analogio.AnalogIn(SENSE_ADC)
addr = []
for p in MUX_PINS:
    d = digitalio.DigitalInOut(p)
    d.direction = digitalio.Direction.OUTPUT
    d.value = False
    addr.append(d)

lasers = []
for gp in LASERS:
    lasers.append(pwmio.PWMOut(getattr(board, "GP%d" % gp), frequency=1000, duty_cycle=0))

def mux(ch):
    for bit in range(3):
        addr[bit].value = bool(ch & (1 << bit))
    time.sleep(0.02)

def read(ch):
    mux(ch)
    return adc.value

def all_off():
    for p in lasers:
        p.duty_cycle = 0

def snap():
    return [read(i) for i in range(8)]

STEP_S = 2.0
DUTIES = (0, 2048, 4096, 8192, 16384, 24576, 32768, 40960, 49152, 57344, 65535)
# filled by the identify phase: laser index -> mux port
SENSE_OF = {0: 0, 1: 1}

def ramp(ch, port):
    print("RAMP ch %d GP%d via mux %d" % (ch, LASERS[ch], port))
    print("duty   count")
    prev = None
    for duty in DUTIES:
        all_off()
        lasers[ch].duty_cycle = duty
        time.sleep(STEP_S)
        v = read(port)
        knee = ""
        if prev is not None and duty:
            dv = v - prev
            if dv < 400:
                knee = "  flat"
            elif prev < 5000 and v - prev > 2000:
                knee = "  rising"
        print("%5d  %5d%s" % (duty, v, knee))
        prev = v
    lasers[ch].duty_cycle = 0

print("laser_sense hold %ss  lasers %s" % (HOLD_S, LASERS))
print("PT outputs should be unplugged")
all_off()
print("all off — measure now")
time.sleep(HOLD_S)
dark = snap()
print("dark", dark)

for ch in ACTIVE:
    all_off()
    lasers[ch].duty_cycle = 65535
    print("ch %d GP%d ON — measure sense wire" % (ch, LASERS[ch]))
    time.sleep(HOLD_S)
    lit = snap()
    print("lit ", lit)
    best, best_d = None, 0
    for i in range(8):
        d = lit[i] - dark[i]
        flag = "FOLLOWS" if d > 2000 else "still"
        print("  mux %d  dark %5d  lit %5d  d %+6d  %s" % (i, dark[i], lit[i], d, flag))
        if d > best_d:
            best, best_d = i, d
    print("ch %d follows mux %s" % (ch, best))
    lasers[ch].duty_cycle = 0
    time.sleep(HOLD_S)
    
for ch, port in SENSE_OF.items():
    ramp(ch, port)
all_off()
print("done")

while True:
    time.sleep(10)