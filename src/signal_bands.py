# signal_bands.py -- measurement scale and pass bands (first guesses, to be
# tuned on the bench).  Leaf: numbers only.
SENSE_OHMS = 15.0
VREF = 3.3
COUNTS_PER_MA = 65535.0 * SENSE_OHMS * 1e-3 / VREF   # ~297.9 counts/mA
FLOOR_COUNTS = 900        # README: empty port / resting head read 500-900

# Loopback plug, path B (Orange -> Green/White), model H, Ron ~0.5-1 k:
#   loaded : GP28 ~0.835*duty, GP26/GP28 ~0.98-0.99; no plug: GP28 ~1.0*duty
LB_RATIO_MIN = 0.93           # GP26/GP28 when looped (Ron up to ~3 k passes)
LB_LOAD_MIN, LB_LOAD_MAX = 0.75, 0.92   # GP28 / unloaded-expected, looped
UNLOADED_MIN = 0.95           # GP28 / expected with no plug
DAC_MAX_RESID = 1311          # 2 % FS: RP2xxx ADC ENOB ~9 bit [inferred]
SWEEP_TOL = 0.12              # fraction-high vs duty, path A sweep
