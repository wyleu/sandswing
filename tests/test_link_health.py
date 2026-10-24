# tests for link_health.py (pure state machine, no board)
from support import *   # fakes, rigs, case/expect, finish
import link_health
from link_health import LinkHealth


def obs(**kw):
    o = {"wifi_up": True, "n_clients": 0, "hb_seen": False, "last_rx_age_s": None,
         "last_gw_ok": None, "gw_fail_count": 0, "tower_inactive": True,
         "quiet_ms": 999999, "watchdog_armed": False}
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
    # a pico reset only when the watchdog is armed
    lh2 = LinkHealth()
    lh2.update(0.0, obs(last_rx_age_s=30.0, **c))
    _, ev = lh2.update(link_health.LINK_DEAD_RESET_S + 1, obs(last_rx_age_s=700.0, **c))
    assert "pico_reset" not in ev


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
