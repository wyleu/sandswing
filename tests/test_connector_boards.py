# tests for connector_boards.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import channel_gate
import connector_boards


@case
def test_no_board_state_and_gate():
    k = {0: "loopback", 1: "head_new", 2: "empty", 3: "fault",
         4: "empty", 5: "empty", 6: "empty", 7: "empty"}
    auto = [dict(connector_boards.DEFAULT_BOARDS[0]), dict(connector_boards.DEFAULT_BOARDS[1], present=None)]
    out = connector_boards.apply_boards(k, auto)
    assert [out[c] for c in range(4, 8)] == ["no_board"] * 4
    assert out[2] == "empty" and out[3] == "fault"
    k2 = dict(k); k2[5] = "head_new"                   # board 2 fitted, one head
    out2 = connector_boards.apply_boards(k2, auto)
    assert out2[4] == "empty" and out2[5] == "head_new"
    out3 = connector_boards.apply_boards(k2)                          # default: conn2 absent
    assert out3[5] == "no_board"
    g = channel_gate.ChannelGate(8)
    for c, v in out.items():
        g.set_kind(c, v)
    assert not any(g.may_drive(c) for c in range(4, 8))


finish()
