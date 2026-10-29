# tests for link_health.py (pure state machine, no board)
from support import *   # fakes, rigs, case/expect, finish
import link_health
from link_health import LinkHealth


def obs(**kw):
    o = {"wifi_up": True, "n_clients": 0, "hb_seen": False, "last_rx_age_s": None,
         "last_gw_ok": None, "gw_fail_count": 0, "tower_inactive": True,
         "quiet_ms": 999999, "gw_failing": False}
    o.update(kw)
    return o


@case
def test_gateway_misses_ask_for_one_radio_reset():
    lh = LinkHealth()
    lh.update(0.0, obs())
    _, ev = lh.update(2.0, obs(last_gw_ok=False, gw_fail_count=4))
    assert "radio_reset" in ev
    _, ev = lh.update(4.0, obs(last_gw_ok=False, gw_fail_count=4))
    assert "radio_reset" not in ev          # already asked: wait for the rejoin


@case
def test_rejoin_with_a_clean_gateway_record_does_not_reset_again():
    # the 21:31-21:35 loop: after wifi_back the old miss count was still 4
    lh = LinkHealth()
    lh.update(0.0, obs())
    lh.update(2.0, obs(last_gw_ok=False, gw_fail_count=4))
    lh.update(4.0, obs(wifi_up=False, last_gw_ok=False, gw_fail_count=4))
    _, ev = lh.update(40.0, obs(last_gw_ok=False, gw_fail_count=0))   # cleared by _wifi_regained
    assert "wifi_up" in ev and "radio_reset" not in ev


@case
def test_silent_client_escalates_to_radio_reset_then_pico_reset():
    lh = LinkHealth()
    c = dict(n_clients=1, hb_seen=True)
    lh.update(0.0, obs(last_rx_age_s=1.0, **c))
    lh.update(30.0, obs(last_rx_age_s=30.0, **c))
    _, ev = lh.update(30.0 + link_health.LINK_DEAD_S, obs(last_rx_age_s=150.0, **c))
    assert "radio_reset" in ev
    # still silent PICO_RESET_S after the spell began: reset the Pico, no watchdog needed
    _, ev = lh.update(30.0 + link_health.PICO_RESET_S, obs(last_rx_age_s=400.0, **c))
    assert "pico_reset" in ev
    _, ev = lh.update(40.0 + link_health.PICO_RESET_S, obs(last_rx_age_s=410.0, **c))
    assert "pico_reset" not in ev            # asked once


@case
def test_silent_spell_with_no_client_resets_once_not_per_rejoin():
    lh = LinkHealth()
    lh.update(0.0, obs(hb_seen=True, last_rx_age_s=1.0))
    _, ev = lh.update(130.0, obs(hb_seen=True, last_rx_age_s=130.0))
    assert "radio_reset" in ev
    lh.update(131.0, obs(wifi_up=False, hb_seen=True, last_rx_age_s=131.0))
    _, ev = lh.update(170.0, obs(hb_seen=True, last_rx_age_s=170.0))   # rejoined, still silent
    assert "radio_reset" not in ev
    lh.update(171.0, obs(hb_seen=True, last_rx_age_s=1.0))             # frames again
    lh.update(172.0, obs(hb_seen=True, last_rx_age_s=1.0))
    _, ev = lh.update(400.0, obs(hb_seen=True, last_rx_age_s=130.0))
    assert "radio_reset" in ev                                          # a new spell may reset


@case
def test_radio_reset_state_settles_so_a_later_reset_is_possible():
    lh = LinkHealth()
    lh.update(0.0, obs())
    _, ev = lh.update(2.0, obs(last_gw_ok=False, gw_fail_count=4))
    assert "radio_reset" in ev
    _, ev = lh.update(100.0, obs(last_gw_ok=False, gw_fail_count=4))
    assert "radio_reset" in ev




@case
def test_wifi_stays_down_resets_the_pico_when_quiet():
    lh = LinkHealth()
    lh.update(0.0, obs(wifi_up=False))
    _, ev = lh.update(link_health.PICO_RESET_S - 1, obs(wifi_up=False))
    assert "pico_reset" not in ev
    _, ev = lh.update(link_health.PICO_RESET_S + 1, obs(wifi_up=False))
    assert "pico_reset" in ev


@case
def test_no_pico_reset_while_the_tower_is_ringing():
    lh = LinkHealth()
    lh.update(0.0, obs(wifi_up=False, tower_inactive=False))
    _, ev = lh.update(link_health.PICO_RESET_S + 60, obs(wifi_up=False, tower_inactive=False))
    assert "pico_reset" not in ev
    _, ev = lh.update(link_health.PICO_RESET_S + 62, obs(wifi_up=False))   # bells stopped
    assert "pico_reset" in ev


@case
def test_wedge_across_radio_resets_is_one_spell():
    # gateway silent -> radio reset -> rejoin with a cleared record -> silent again
    lh = LinkHealth()
    lh.update(0.0, obs(last_gw_ok=False, gw_fail_count=4, gw_failing=True))
    lh.update(5.0, obs(wifi_up=False, gw_failing=True))
    lh.update(40.0, obs(gw_failing=False))           # rejoined, record cleared
    lh.update(70.0, obs(gw_failing=True, gw_fail_count=1, last_gw_ok=False))
    _, ev = lh.update(link_health.PICO_RESET_S + 1, obs(gw_failing=True, gw_fail_count=9,
                                                      last_gw_ok=False))
    assert "pico_reset" in ev


@case
def test_a_working_gateway_ends_the_spell():
    lh = LinkHealth()
    lh.update(0.0, obs(wifi_up=False))
    lh.update(100.0, obs(last_gw_ok=True))           # ping answered
    _, ev = lh.update(link_health.PICO_RESET_S + 50, obs(last_gw_ok=True))
    assert "pico_reset" not in ev


@case
def test_blip_with_no_client_and_a_router_that_ignores_pings_is_not_a_wedge():
    lh = LinkHealth()
    lh.update(0.0, obs(wifi_up=False))
    for t in range(10, 600, 10):                     # up again, nothing to hear
        _, ev = lh.update(float(t), obs())
        assert "pico_reset" not in ev


@case
def test_a_live_client_ends_the_spell():
    lh = LinkHealth()
    lh.update(0.0, obs(wifi_up=False))
    c = dict(n_clients=1, hb_seen=True, last_rx_age_s=1.0)
    lh.update(60.0, obs(**c))
    _, ev = lh.update(link_health.PICO_RESET_S + 50, obs(**c))
    assert "pico_reset" not in ev


@case
def test_a_working_ping_does_not_hide_a_silent_client():
    # the radio answers ping but the server/ws is dead: clients silent for good
    lh = LinkHealth()
    c = dict(n_clients=1, hb_seen=True, last_gw_ok=True)
    lh.update(0.0, obs(last_rx_age_s=1.0, **c))
    lh.update(30.0, obs(last_rx_age_s=30.0, **c))
    _, ev = lh.update(30.0 + link_health.PICO_RESET_S + 1, obs(last_rx_age_s=400.0, **c))
    assert "pico_reset" in ev


@case
def test_repeated_stalls_ask_for_a_radio_reset_even_with_live_clients():
    # 2026-10-26: clients kept reconnecting through 10 s send stalls for 12 minutes
    lh = LinkHealth()
    c = dict(n_clients=1, hb_seen=True, last_rx_age_s=1.0)
    _, ev = lh.update(0.0, obs(stalls=2, **c))
    assert "radio_reset" not in ev            # two is not yet a pattern
    _, ev = lh.update(5.0, obs(stalls=3, quiet_ms=1000, **c))
    assert "radio_reset" not in ev            # a stroke is still happening
    _, ev = lh.update(10.0, obs(stalls=3, **c))
    assert "radio_reset" in ev
    _, ev = lh.update(11.0, obs(stalls=3, **c))
    assert "radio_reset" not in ev            # asked once, wait for the rejoin


@case
def test_stalls_never_reset_while_the_tower_rings():
    lh = LinkHealth()
    _, ev = lh.update(0.0, obs(stalls=5, tower_inactive=False, n_clients=1,
                               hb_seen=True, last_rx_age_s=1.0))
    assert "radio_reset" not in ev


# --- send path (SendPath): table-driven backoff for slow /ws sends ----------

from link_health import SendPath, SEND_OK, SEND_BACKOFF, SEND_DEGRADED


def bad(sp, now, kind="replay"):
    # slow, but short of STARVED_S (a 10 s lwIP sleep is SEND_DRAIN: see below)
    return sp.observe(now, kind, link_health.SLOW_S[kind] + 0.5, True)


@case
def test_send_path_starts_ok_and_accepts_a_full_replay():
    sp = SendPath()
    p = sp.plan(0.0)
    assert p.state == SEND_OK and p.accept and p.replay_n == link_health.SEND_LEVELS[0][1]
    assert p.poll_s <= 1.0 and p.chunk_frames >= 1


@case
def test_fast_sends_never_leave_ok():
    sp = SendPath()
    for t in range(100):
        assert not sp.observe(float(t), "replay", 0.01, True)
        assert not sp.observe(float(t), "live", 0.05, True)
    assert sp.plan(100.0).state == SEND_OK and sp.take_events() == []


@case
def test_slow_or_failed_send_enters_backoff_and_refuses_new_clients():
    sp = SendPath()
    assert bad(sp, 5.0)
    p = sp.plan(5.0)
    assert p.state == SEND_BACKOFF and not p.accept
    assert 1.0 <= p.retry_after_s <= link_health.SEND_LEVELS[1][0]
    sp2 = SendPath()
    assert sp2.observe(1.0, "live", 0.0, False)              # raised: bad whatever the time
    assert sp2.plan(1.0).state == SEND_BACKOFF


@case
def test_every_kind_has_its_own_slow_threshold():
    for kind, limit in link_health.SLOW_S.items():
        sp = SendPath()
        assert not sp.observe(0.0, kind, limit * 0.5, True), kind
        assert sp.observe(0.0, kind, limit, True), kind


@case
def test_hold_over_moves_to_degraded_with_a_smaller_replay_after_repeats():
    sp = SendPath()
    bad(sp, 0.0)
    hold1 = link_health.SEND_LEVELS[1][0]
    assert sp.plan(hold1 - 0.1).state == SEND_BACKOFF
    p = sp.plan(hold1)
    assert p.state == SEND_DEGRADED and p.accept and p.level == 1
    bad(sp, hold1 + 0.5)                                      # the retry fails too
    p = sp.plan(hold1 + 0.5)
    assert p.state == SEND_BACKOFF and p.level == 2


@case
def test_backoff_grows_to_a_cap_and_replay_shrinks_to_none():
    sp = SendPath()
    now, holds, replays = 0.0, [], []
    for _ in range(10):                                       # every retry fails at once
        bad(sp, now)
        p = sp.plan(now)
        holds.append(link_health.SEND_LEVELS[p.level][0])
        now += holds[-1]
        replays.append(sp.plan(now).replay_n)
    assert holds[:6] == [h[0] for h in link_health.SEND_LEVELS[1:]] + [link_health.SEND_LEVELS[-1][0]]
    assert holds == sorted(holds) and max(holds) == link_health.SEND_LEVELS[-1][0] <= 30.0
    assert replays == sorted(replays, reverse=True) and replays[-1] == 0
    assert sp.level == link_health.SEND_MAX_LEVEL                # capped, not unbounded


@case
def test_refusal_window_is_cheap_to_ask_and_never_extended_by_failing_old_clients():
    sp = SendPath()
    bad(sp, 0.0)
    until = sp.hold_until
    for t in (0.1, 0.2, 0.5):
        bad(sp, t)                                            # old clients still timing out
    assert sp.hold_until == until and sp.level == 1


@case
def test_calm_steps_the_level_down_to_ok_and_a_new_bad_send_climbs_again():
    sp = SendPath()
    bad(sp, 0.0)
    bad(sp, 2.5)                                              # level 2
    assert sp.level == 2
    t = sp.hold_until
    t += link_health.SEND_LEVELS[2][4]
    assert sp.plan(t).level == 1                              # calm_s of level 2 elapsed
    t += link_health.SEND_LEVELS[1][4]
    p = sp.plan(t)
    assert p.state == SEND_OK and p.level == 0 and p.replay_n == link_health.SEND_LEVELS[0][1]
    evs = [w for w, _ in sp.take_events()]
    assert evs[-1] == "send_ok" and "send_calmer" in evs
    bad(sp, t + 1)
    assert sp.plan(t + 1).level == 1


@case
def test_calm_does_not_run_while_bad_sends_keep_coming():
    sp = SendPath()
    t = 0.0
    for _ in range(6):
        bad(sp, t)
        t += link_health.SEND_LEVELS[sp.level][0] + 1.0
        sp.plan(t)
    for _ in range(3):
        t += link_health.SEND_LEVELS[-1][4] - 1.0             # never quite calm
        bad(sp, t)
        sp.plan(t + 40.0)
    assert sp.level == link_health.SEND_MAX_LEVEL


@case
def test_events_say_what_happened_in_link_log_words():
    sp = SendPath()
    bad(sp, 0.0)
    sp.plan(10.0)
    evs = sp.take_events()
    assert [w for w, _ in evs] == ["send_backoff", "send_degraded"]
    info = dict(evs)["send_backoff"]
    assert info["level"] == 1 and info["hold_s"] > 0 and "replay slow" in info["why"]
    assert sp.take_events() == []                             # taken once


@case
def test_send_table_is_complete_and_consistent():
    states = {SEND_OK, SEND_BACKOFF, SEND_DEGRADED, link_health.SEND_DRAIN}
    assert set(link_health.SEND_ACCEPTS) == states
    assert set(link_health.SEND_SERVES) == set(link_health.SEND_WRITES) == states
    for (st, ev), (nxt, delta) in link_health.SEND_TRANSITIONS.items():
        assert st in states and nxt in states and ev in (
            "bad", "starved", "drained", "hold_over", "calm", "level0")
    rows = link_health.SEND_LEVELS
    holds = [r[0] for r in rows]
    replays = [r[1] for r in rows]
    assert holds == sorted(holds) and replays == sorted(replays, reverse=True)
    assert rows[0][0] == 0.0 and rows[-1][0] <= 30.0 and rows[-1][1] == 0
    assert all(r[2] >= 1 for r in rows[:-1]) and all(r[3] > 0 for r in rows)
    # every bound a blocking wait sits under fits in the per-poll budget
    assert link_health.REQ_BOUND_S + link_health.REPLY_BOUND_S + link_health.SEND_TIMEOUT_S \
        + link_health.CLOSE_TIMEOUT_S <= link_health.POLL_BUDGET_S + 0.2


@case
def test_doc_diagram_lists_every_send_transition():
    import os
    doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "link-health.md")).read()
    block = doc[doc.index("stateDiagram-v2", doc.index("## Send path")):]
    block = block[:block.index("```")]
    for (st, ev), (nxt, delta) in link_health.SEND_TRANSITIONS.items():
        if st != nxt or delta:
            assert "%s --> %s" % (st, nxt) in block, (st, nxt)


# --- ClientPath: one machine per /ws client --------------------------------

@case
def test_client_path_every_open_state_cuts_a_client_that_misses_a_deadline():
    for st in (link_health.C_NEW, link_health.C_LIVE, link_health.C_LAGGING):
        c = link_health.ClientPath("10.0.0.1")
        c.state = st
        assert c.fire("overran") == link_health.CUT and c.state == link_health.C_GONE, st
    c = link_health.ClientPath()
    assert c.fire("overran") == link_health.CUT
    assert c.fire("overran") is None and c.state == link_health.C_GONE   # gone stays gone


@case
def test_client_path_quiet_holds_and_heard_resumes():
    c = link_health.ClientPath("10.0.0.1")
    assert c.fire("heard") is None and c.state == link_health.C_LIVE and c.writes()
    assert c.fire("quiet") == link_health.HOLD and c.state == link_health.C_LAGGING
    assert not c.writes()                     # nothing more is queued in lwIP for it
    assert c.fire("heard") == link_health.RESUME and c.writes()


@case
def test_client_path_only_a_lagging_client_is_blamed_or_replaced():
    # Sandbells ingest and the page-3 widget can share one address: a talking
    # client is never cut for lwIP starving or for a newer connection
    for ev in ("starved", "superseded"):
        for st in (link_health.C_NEW, link_health.C_LIVE):
            c = link_health.ClientPath("10.0.0.1")
            c.state = st
            assert c.fire(ev) is None and c.state == st, (st, ev)
        c = link_health.ClientPath("10.0.0.1")
        c.state = link_health.C_LAGGING
        assert c.fire(ev) == link_health.CUT, ev


@case
def test_client_path_errors_close_politely_but_not_reading_is_cut():
    assert link_health.CLIENT_TRANSITIONS[(link_health.C_LIVE, "failed")][1] == link_health.CLOSE
    assert link_health.CLIENT_TRANSITIONS[(link_health.C_LIVE, "closed")][1] == link_health.CLOSE
    for ev in ("silent", "overran", "backlog", "starved", "superseded"):
        assert link_health.CLIENT_TRANSITIONS[(link_health.C_LAGGING, ev)][1] == link_health.CUT, ev


@case
def test_client_constants_come_from_the_wire_and_the_lwip_source():
    assert link_health.CLIENT_LAG_S == 2 * link_health.CLIENT_HB_S == 10.0
    assert link_health.CLIENT_LAG_S < link_health.CLIENT_SILENT_S
    assert abs(link_health.LWIP_ERR_MEM_S - 10.0) < 1e-9
    # the starved mark sits above every Python-side bound and below the C sleep
    assert link_health.STARVED_S > link_health.REQ_BOUND_S + link_health.REPLY_BOUND_S
    assert link_health.STARVED_S > link_health.SEND_TIMEOUT_S
    assert link_health.STARVED_S < link_health.LWIP_ERR_MEM_S


@case
def test_doc_diagram_lists_every_client_transition():
    import os
    doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "link-health.md")).read()
    block = doc[doc.index("stateDiagram-v2", doc.index("## Per-client path")):]
    block = block[:block.index("```")]
    for (st, ev), (nxt, action) in link_health.CLIENT_TRANSITIONS.items():
        assert "%s --> %s: %s" % (st, nxt, ev) in block, (st, ev, nxt)


@case
def test_answered_stalls_never_reach_the_radio_reset():
    # farm_ws does not count a stall the client model answered; with none
    # counted, LinkHealth never asks for a radio reset however many happen
    lh = LinkHealth()
    for t in range(0, 300, 30):
        _, ev = lh.update(float(t), obs(n_clients=2, hb_seen=True, last_rx_age_s=1.0,
                                        stalls=0))
        assert "radio_reset" not in ev
    _, ev = lh.update(301.0, obs(n_clients=2, hb_seen=True, last_rx_age_s=1.0,
                                 stalls=link_health.STALL_N))
    assert "radio_reset" in ev               # unanswered stalls still do, as before



@case
def test_one_call_stuck_in_lwip_drains_instead_of_serving_again():
    sp = SendPath()
    assert sp.plan(0.0).serve and sp.plan(0.0).writes
    sp.observe(10.06, "server", 10.06, True)          # 14:45: server 10.1 s, 0 clients
    p = sp.plan(10.1)
    assert p.state == link_health.SEND_DRAIN and not p.serve and not p.writes and not p.accept
    ev = dict(sp.take_events())
    assert ev["send_drain"]["hold_s"] == link_health.DRAIN_S and "server slow" in ev["send_drain"]["why"]
    assert sp.plan(10.06 + link_health.DRAIN_S - 0.1).state == link_health.SEND_DRAIN
    p = sp.plan(10.06 + link_health.DRAIN_S)
    assert p.state == SEND_DEGRADED and p.serve and p.writes and p.accept and p.level == 1


@case
def test_a_merely_slow_send_still_backs_off_not_drains():
    sp = SendPath()
    sp.observe(1.0, "server", 1.2, True)              # slow, but no C sleep
    assert sp.plan(1.0).state == SEND_BACKOFF and sp.plan(1.0).serve


@case
def test_drain_constants_come_from_the_lwip_source():
    assert link_health.DRAIN_S >= link_health.CLOSE_ABORT_S == 10.0
    assert link_health.DRAIN_S - link_health.CLOSE_ABORT_S <= 2 * link_health.TCP_POLL_STEP_S


@case
def test_reply_path_single_writer_table():
    r = link_health.ReplyPath()
    assert r.fire("sent") is None and r.state == link_health.R_WAITING   # not its turn yet
    assert r.fire("turn") == "write" and r.state == link_health.R_SENDING
    assert r.fire("blocked") == "yield" and r.fire("sent") == "write"
    assert r.fire("done") == "close" and r.state == link_health.R_DONE
    for ev in ("deadline", "failed", "starved"):
        for st in (link_health.R_WAITING, link_health.R_SENDING):
            r = link_health.ReplyPath()
            r.state = st
            assert r.fire(ev) == "abort" and r.state == link_health.R_CANCELLED, (st, ev)
    r = link_health.ReplyPath()
    assert r.fire("overflow") == "abort"


@case
def test_reply_constants_come_from_lwipopts_and_the_sandbells_proxy():
    assert link_health.REPLY_CHUNK_B == link_health.TCP_MSS == 1460
    assert link_health.REPLY_DEADLINE_S < 3.0          # Sandbells STATUS_TRY_TIMEOUT_S
    assert link_health.STARVED_S < link_health.LWIP_ERR_MEM_S


@case
def test_a_failed_close_is_not_a_bad_send_but_a_slow_one_is():
    sp = SendPath()
    assert not sp.observe(1.0, "close", 0.0, False)       # EBADF at once: nothing about the link
    assert sp.plan(1.0).state == SEND_OK
    assert sp.observe(1.0, "close", link_health.SLOW_S["close"], False)
    assert sp.plan(1.0).state == SEND_BACKOFF
    assert SendPath().observe(1.0, "live", 0.0, False)    # other kinds: a failure still counts


@case
def test_doc_diagram_lists_every_reply_transition():
    import os
    doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "link-health.md")).read()
    block = doc[doc.index("stateDiagram-v2", doc.index("## /status reply")):]
    block = block[:block.index("```")]
    for (st, ev), (nxt, action) in link_health.REPLY_TRANSITIONS.items():
        assert "%s --> %s: %s" % (st, nxt, ev) in block, (st, ev, nxt)


@case
def test_status_gate_holds_while_the_tower_rings():
    g = link_health.StatusGate()
    assert g.fire("inactive") is None and g.state == link_health.STATUS_SERVE
    assert g.fire("active") == "hold" and g.state == link_health.STATUS_HELD
    assert g.fire("ringing_down") is None and g.state == link_health.STATUS_HELD
    assert g.fire("active") is None
    assert g.fire("bogus") is None and g.state == link_health.STATUS_HELD
    assert g.fire("inactive") == "resume" and g.state == link_health.STATUS_SERVE
    assert g.fire("ringing_down") == "hold"                  # amber alone holds too
    for st in (link_health.R_WAITING, link_health.R_SENDING):
        r = link_health.ReplyPath()
        r.state = st
        assert r.fire("held") == "abort"


@case
def test_doc_diagram_lists_every_status_gate_transition():
    import os
    doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "link-health.md")).read()
    block = doc[doc.index("stateDiagram-v2", doc.index("### /status while the tower rings")):]
    block = block[:block.index("```")]
    for (st, ev), (nxt, action) in link_health.STATUS_TRANSITIONS.items():
        assert "%s --> %s: %s" % (st, nxt, ev) in block, (st, ev, nxt)


@case
def test_existing_link_states_are_unchanged_by_the_send_path():
    # the Wi-Fi/client machine does not know about SendPath: same labels, same flow
    lh = LinkHealth()
    assert lh.update(0.0, obs(wifi_up=False))[0] == link_health.DOWN
    assert lh.update(1.0, obs())[0] == link_health.UP
    assert lh.update(2.0, obs(n_clients=1, hb_seen=True, last_rx_age_s=1.0))[0] == link_health.CLIENT_LIVE
    assert lh.update(30.0, obs(n_clients=1, hb_seen=True, last_rx_age_s=30.0))[0] == link_health.CLIENT_SILENT


finish()
