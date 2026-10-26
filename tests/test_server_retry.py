# tests for Concentrator._ensure_server: Wi-Fi up but nothing listening
from support import *   # fakes, rigs, case/expect, finish
import os
import sys
import types
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "fakes"), os.path.join(HERE, "fakes", "stubs")]
import concentrator
import farm_ws


@case
def test_server_is_started_when_wifi_is_up_but_nothing_listens():
    # 2026-10-26: the link dropped between the join and start_server, came back
    # before link_step looked, and the Pico never listened again
    started, events = [], []
    saved = (farm_ws.server_running, concentrator.start_server)
    farm_ws.server_running = lambda: bool(started)
    concentrator.start_server = lambda cfg, fn: started.append(1)
    app = types.SimpleNamespace(cfg=None, status_payload=None,
                                link_event=lambda what, **kw: events.append(what))
    state = {}
    try:
        concentrator.Concentrator._ensure_server(app, 100.0, state)
        assert len(started) == 1 and events == ["server_retry"]
        concentrator.Concentrator._ensure_server(app, 101.0, state)
        assert len(started) == 1                       # running now: leave it alone
        del started[:]
        concentrator.Concentrator._ensure_server(app, 102.0, state)
        assert not started                              # too soon after the last try
        concentrator.Concentrator._ensure_server(app, 100.0 + concentrator.SERVER_RETRY_S, state)
        assert len(started) == 1
    finally:
        farm_ws.server_running, concentrator.start_server = saved


finish()
