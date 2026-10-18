# laser_measure.py -- laser present / current band from the GP26 step.
# Secondary check (the 15 R exists mainly for loopback testing).
from signal_bands import COUNTS_PER_MA, FLOOR_COUNTS
from adc_avg import avg


def counts_to_ma(counts):
    return counts / COUNTS_PER_MA


def classify_laser(dark, on, expected_ma=5.0, ref_ma=None, floor=FLOOR_COUNTS,
                   max_ma=60.0, drift_frac=0.35):
    """dark/on = GP26 counts with laser duty 0 / 65535 (DC), averaged.
    Returns (status, i_on_ma, i_dark_ma); status ok/stuck_on/open/over/drift.
    NOTE: README bench step (~27 mA) does not match 5 mA modules: set
    expected_ma / ref_ma from a commissioning run."""
    i_dark = counts_to_ma(dark)
    i_on = counts_to_ma(on - dark)
    if dark > floor + 0.4 * expected_ma * COUNTS_PER_MA:
        return "stuck_on", i_on, i_dark
    if i_on < 0.4 * expected_ma:
        return "open", i_on, i_dark
    if i_on > max_ma:
        return "over", i_on, i_dark
    if ref_ma and abs(i_on - ref_ma) > drift_frac * ref_ma:
        return "drift", i_on, i_dark
    return "ok", i_on, i_dark


def measure_laser(tower, bell, laser_pwm, adc, settle=None, n=16):
    """With the bell selected: dark average, laser full (DC), average.
    Leaves the laser OFF.  TODO(spec C2): settle from RC, not 0.05 s."""
    sleep = tower.pins.sleep
    settle = 0.05 if settle is None else settle
    with tower.select(bell):
        laser_pwm.duty_cycle = 0
        sleep(settle)
        dark = avg(adc, n)
        laser_pwm.duty_cycle = 65535
        sleep(settle)
        on = avg(adc, n)
        laser_pwm.duty_cycle = 0
    return dark, on
