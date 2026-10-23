# tests for socket_classify.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import socket_classify


@case
def test_classify_signatures():
    C = socket_classify.classify_socket
    assert C(sig(l50=0.835, r50=0.99, follow=True))[0] == "loopback"
    assert C(sig(di_ma=5.0, thr_toggle=True))[0] == "head_new"
    assert C(sig(di_ma=5.0, pull_follow=False))[0] == "head_old"
    assert C(sig())[0] == "empty"
    faults = [
        sig(l0=0.5),                                  # Orange to +V
        sig(l50=0.01),                                # Orange to GND
        sig(dark=4000),                               # Green/White to +V
        sig(di_ma=5.0, x=0.1),                        # crossed pairs
        sig(di_ma=5.0, r50=0.99, l50=0.84),           # inter-pair short
        sig(follow=True),                             # strap B open
        sig(l50=0.33, pull_follow=False),             # Orange<->Blue/White swap
        sig(di_ma=5.0),                               # sense conductor open
        sig(pull_follow=False),                       # pin 7 / pin 6 open
        sig(thr_toggle=True),                         # new head, no laser
    ]
    for f in faults:
        k, why = C(f)
        assert k == "fault", (f, k, why)


TRIGGER = dict(l50=0.835, r50=0.99)       # Orange<->Green/White, switch open


@case
def test_trigger_plug_switch_open_is_trigger_not_fault():
    C = socket_classify.classify_socket
    for extra in ({}, {"sense_hi": True, "pull_follow": False},
                  {"pull_follow": False}):           # switch to 0 V held
        k, why = C(sig(**dict(TRIGGER, **extra)))
        assert k == "trigger", (extra, k, why)
        text = " ".join(why).lower()
        for bad in ("fault", "partial", "short", "loopback", "open conductor"):
            assert bad not in text, (bad, why)


@case
def test_trigger_does_not_swallow_neighbours():
    C = socket_classify.classify_socket
    assert C(sig(l50=0.835, r50=0.99, follow=True))[0] == "loopback"   # held
    assert C(sig(follow=True))[0] == "fault"              # strap A only
    assert C(sig(l50=0.33, r50=0.99))[0] == "fault"       # 10 k band
    assert C(sig(l50=0.60, r50=0.99))[0] == "fault"       # below trigger load
    assert C(sig(l50=0.835, r50=0.99, di_ma=5.0))[0] == "fault"  # + laser
    k, why = C(sig(l50=0.835, r50=0.99, pull_follow=False))
    assert k == "trigger" and "held" in why[0], (k, why)
    assert C(sig())[0] == "empty"
    assert C(sig(sense_hi=True, pull_follow=False))[0] == "empty"


@case
def test_ethernet_magnetics_signature_is_fault():
    k, why = socket_classify.classify_socket(sig(di_ma=200.0, pull_follow=False))
    assert k == "fault" and "magnetics" in why[0], (k, why)


finish()
