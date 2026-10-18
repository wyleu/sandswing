"""
fake_strip.py
=============
Synthetic 2-1-4 wheel-tape edges for decoder tests.

Filename: tests/fake_strip.py
Runs on:  CPython. No Pico, no GPIO.

TAPE MODEL
    Seven equal units on the wheel:
        2 reflect + 1 dark + 4 reflect
    Rest of the wheel is dark (wheel=False).

    direction "cw"  : observer sees 2 then 4
    direction "acw" : observer sees 4 then 2

    pass_ms = time for the whole 7-unit tape to cross the beam.
    unit_ms = pass_ms / 7.

    If the tape subtends strip_deg degrees of the wheel and one
    whole revolution (or one stroke) takes T_ms:

        pass_ms = strip_deg / 360.0 * T_ms     # one revolution
        # or, more often for a stroke:
        pass_ms = strip_deg / 360.0 * 2 * T_ms # if T_ms is handstroke period
    Tests usually just pick pass_ms directly (e.g. 70).

PUBLIC
    strip_edges(pass_ms, direction="cw", t0=0)
        yield (t_ms, wheel_bool) at each band edge, then wheel False
    swing(T_ms=2500, pass_ms=70, t0=0)
        one cw pass, wait until T_ms/2, one acw pass
    samples(edges, step_ms=1, t_end=None)
        expand edges into a regular (t, wheel) stream for a decoder that
        wants a level every millisecond

DOES NOT
    Decode direction. That is strip_decoder.py.
    Talk to BellMachine. Tests wire the two together later.
"""


def strip_edges(pass_ms, direction="cw", t0=0):
    if pass_ms <= 0:
        raise ValueError("pass_ms must be > 0")
    if direction not in ("cw", "acw"):
        raise ValueError("direction must be cw or acw")
    unit = float(pass_ms) / 7.0
    bands = (2, 1, 4) if direction == "cw" else (4, 1, 2)
    t = float(t0)
    shine = True
    for width in bands:
        yield (int(round(t)), shine)
        t += width * unit
        shine = not shine
    yield (int(round(t)), False)


def swing(T_ms=2500, pass_ms=70, t0=0):
    t0 = int(t0)
    for ev in strip_edges(pass_ms, "cw", t0=t0):
        yield ev
    mid = t0 + int(T_ms) // 2
    last_t = mid
    for ev in strip_edges(pass_ms, "acw", t0=mid):
        last_t = ev[0]
        yield ev
    return last_t


def samples(edges, step_ms=1, t_end=None):
    edges = list(edges)
    if not edges:
        return
    if t_end is None:
        t_end = edges[-1][0]
    level = False
    ei = 0
    t = edges[0][0]
    while t <= t_end:
        while ei < len(edges) and edges[ei][0] <= t:
            level = edges[ei][1]
            ei += 1
        yield (t, level)
        t += step_ms