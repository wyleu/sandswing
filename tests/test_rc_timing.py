# tests for rc_timing.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import rc_timing


@case
def test_rc_tau_and_settle_numbers():
    tau = rc_timing.rc2_tau_s(10e3, 100e-9, 10e3, 1e-6)
    assert abs(tau - 0.0205) < 0.0005, tau
    full = rc_timing.rc_settle_s(tau, 1.0, 1 / 4096.0)
    assert 0.16 < full < 0.18, full            # sim: 171 ms to 1/4096
    assert rc_timing.rc_settle_s(tau, 0.0001, 1 / 4096.0) == 0.0


finish()
