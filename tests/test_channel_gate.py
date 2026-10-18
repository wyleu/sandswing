# tests for channel_gate.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import channel_gate


@case
def test_channel_gate():
    g = channel_gate.ChannelGate(4)
    assert not any(g.may_drive(i) for i in range(4))     # unknown at boot
    g.set_kind(0, "loopback"); g.set_kind(1, "head_new")
    g.set_kind(2, "fault"); g.set_kind(3, "empty")
    assert [g.may_drive(i) for i in range(4)] == [True, True, False, False]
    assert g.set_kind(2, "head_old") == "fault"           # latched
    g.clear(2); g.set_kind(2, "head_old")
    assert g.may_drive(2)

    class B:
        def __init__(s, i):
            s.index = i
            s.laser = FakePWM(65535)
    bs = [B(i) for i in range(4)]
    g.enforce(bs)
    assert [b.laser.duty_cycle for b in bs] == [65535, 65535, 65535, 0]


finish()
