# adc_avg.py -- average n ADC reads. Leaf.


def avg(adc, n):
    s = 0
    for _ in range(n):
        s += adc.value
    return s // n


def paced_avg(adc, n, window_ns, now_ns):
    """Mean of up to n reads spaced evenly over window_ns (the first half a
    step in), after one discarded read (the first conversion after the mux
    moves is stale).  For a chopped signal (the laser PWM) make window_ns a
    whole number of periods and n coprime with that number: the sample
    phases then tile the period, so the mean is the average whatever phase
    the burst starts at.  A late sample is taken at once; nothing is read
    after window_ns (so the window stays whole periods on a slow loop).
    now_ns: () -> int ns (time.monotonic_ns).  -> (mean counts, reads,
    elapsed ns)."""
    _ = adc.value
    step = window_ns // n
    t0 = now_ns()
    end = t0 + window_ns
    due = t0 + step // 2
    s = k = 0
    t = t0
    while k < n:
        t = now_ns()
        if t < due:
            continue
        if t >= end:
            break
        s += adc.value
        k += 1
        due += step
    if k == 0:                     # a clock that jumped past the window
        s, k = adc.value, 1
    return s // k, k, now_ns() - t0
