# socket_survey.py -- take the classify_socket measurements for each bell
# and update the ChannelGate (passed in).  Bells need .laser (PWMOut) and
# .sense_pin (DigitalInOut) attached by concentrator.py.
# TODO(spec C4): the 0.05 s / 0.002 s sleeps -> derived, checked waits.
from loopback_test import LoopbackSelfTest
from socket_classify import classify_socket
from laser_measure import counts_to_ma
from connector_boards import DEFAULT_BOARDS, board_of, apply_boards
from adc_avg import avg as _avg


class SocketSurvey(LoopbackSelfTest):
    """Collects the measurements above for one bell, then classifies."""

    def __init__(self, tower, pull_up=None, pull_down=None, **kw):
        LoopbackSelfTest.__init__(self, tower, **kw)
        if pull_up is None:
            import digitalio
            pull_up, pull_down = digitalio.Pull.UP, digitalio.Pull.DOWN
        self.pull_up, self.pull_down = pull_up, pull_down

    def measure(self, bell, sweep=(0, 16384, 32768, 49152, 65535)):
        d, adc = bell.drive, bell.sense.adc
        m = {}
        with self.tower.select(bell):
            bell.laser.duty_cycle = 0
            r28, r26 = self._thr(bell, 0)
            m["l0"] = r28 / 65535.0
            m["dark"] = r26
            bell.laser.duty_cycle = 65535
            self._sleep(0.05)
            on = _avg(adc, self.n_avg)
            r28_on = _avg(d.back, self.n_avg)
            bell.laser.duty_cycle = 0
            self._sleep(0.05)
            m["di_ma"] = counts_to_ma(on - r26)
            m["x"] = abs(r28_on - r28) / 65535.0
            r28, r26 = self._thr(bell, 32768)
            exp = d.expected(32768)
            m["l50"] = r28 / float(exp) if exp else 0.0
            m["r50"] = r26 / float(r28) if r28 else 0.0
            seen = []
            for duty in sweep:
                self._thr(bell, duty)
                seen.append(bool(bell.sense_pin.value))
            m["thr_toggle"] = len(set(seen)) > 1
            d.off()
        m["follow"] = self.digital_loopback(bell)["ok"]
        with self.tower.select(bell):
            p = bell.sense_pin
            old = p.pull
            p.pull = self.pull_up
            self._sleep(0.002)
            up = bool(p.value)
            p.pull = self.pull_down
            self._sleep(0.002)
            dn = bool(p.value)
            p.pull = old
        m["pull_follow"] = up and not dn
        m["sense_hi"] = up and dn     # held high by the board's 4.7k pull-up

        return m

    def classify(self, bell, expected_ma=5.0):
        m = self.measure(bell)
        kind, why = classify_socket(m, expected_ma)
        return {"ch": bell.index, "kind": kind, "why": why, "m": m}


def survey_all(survey, bells, gate, boards=DEFAULT_BOARDS, expected_ma=5.0):
    """Classify every bell; never probe a channel whose board is configured
    absent (no drive at all).  Updates the gate.  Returns {ch: report}."""
    rep = {}
    for b in bells:
        brd = board_of(b.index, boards)
        if brd is not None and brd.get("present") is False:
            rep[b.index] = {"ch": b.index, "kind": "no_board",
                            "why": ["%s configured absent" % brd["name"]]}
            continue
        rep[b.index] = survey.classify(b, expected_ma)
    kinds = apply_boards({c: r["kind"] for c, r in rep.items()}, boards)
    for c, k in kinds.items():
        if k == "no_board" and rep[c]["kind"] != "no_board":
            rep[c]["why"] = ["no connector board detected (all channels empty)"]
        rep[c]["kind"] = k
        gate.set_kind(c, k)
    return rep
