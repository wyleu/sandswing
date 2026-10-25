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


finish()


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
