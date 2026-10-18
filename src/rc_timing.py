# rc_timing.py -- settle and refresh times derived from R and C. Leaf.
import math


def rc2_tau_s(r1, c1, r2, c2):
    """Dominant time constant of the loaded 2-stage RC (R1-C1 then R2-C2).
    Poles of  a2 s^2 + a1 s + 1  with a2=R1C1R2C2, a1=R1C1+R2C2+R1C2.
    10k/100n + 10k/1u -> ~20.5 ms (vs 10 ms for the second stage alone)."""
    a2 = r1 * c1 * r2 * c2
    a1 = r1 * c1 + r2 * c2 + r1 * c2
    d = math.sqrt(a1 * a1 - 4.0 * a2)
    return -1.0 / ((-a1 + d) / (2.0 * a2))


def rc_settle_s(tau_s, step_frac, tol_frac, floor_s=0.0):
    """Time for a step of step_frac (of full scale) to come within tol_frac.
    Single dominant pole; add ~0.5 ms for the fast pole via floor_s."""
    step_frac = abs(step_frac)
    if step_frac <= tol_frac:
        return floor_s
    return tau_s * math.log(step_frac / tol_frac) + floor_s


def refresh_interval_s(tol_v=0.005, bias_a=25e-9, hold_c=100e-9):
    """Datasheet estimate: longest gap between refreshes of one hold cap
    for droop <= tol_v.  Cross-check only; the user's scope-measured
    interval wins (threshold_mux.refresh.interval_s)."""
    return hold_c * tol_v / bias_a
