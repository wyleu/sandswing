# tests for panel.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import panel


@case
def test_panel_pixels_layout():
    px = panel.panel_pixels({0: "loopback", 1: "head_new", 2: "fault"},
                        link="ws_client", activity="ringing")
    assert len(px) == 6
    assert px[0] == panel.LED_LINK["ws_client"] and px[5] == panel.LED_ACTIVITY["ringing"]
    assert px[1] == panel.LED_KIND["loopback"] and px[3] == panel.LED_KIND["fault"]
    assert px[4] == panel.LED_KIND["unknown"]


finish()
