# laser_sense.py — bench check: which 4051 port follows which laser
# Hold each state for 5 s. Not for the tower. Unplug PT outputs first.
import time
import board
import pwmio
import analogio
import digitalio

HOLD_S = 5.0
LASERS = (8, 9, 10, 7)          # ch 0..3, change to match settings.json
MUX_PINS = (board.GP11, board.GP12, board.GP13)  # A, B, C — edit
SENSE_ADC = board.GP28          # 4051 Z
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

all_off()
print("done")
while True:
    time.sleep(10)