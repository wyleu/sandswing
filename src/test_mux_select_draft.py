# Plain CPython tests for mux_select_draft.py with fake pins.
#   python3 test_mux_select_draft.py
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mux_select_draft as M


class FakePinBank:
    """Emulates CircuitPython: claiming a claimed GP raises ValueError."""
    def __init__(self):
        self.claimed = {}
        self.log = []
        self.slept = []

    def _claim(self, gp, obj):
        gp = int(gp)
        if gp in self.claimed:
            raise ValueError("GP%d in use" % gp)
        self.claimed[gp] = obj
        obj.gp = gp
        obj.bank = self
        return obj

    def output(self, gp, value=False):
        return self._claim(gp, FakeOut(value))

    def analog_in(self, gp):
        adc = FakeADC()
        if int(gp) == 28:                      # RC node on output-4051 COMMON
            adc = FakeRCNode(self)
        return self._claim(gp, adc)

    def pwm_out(self, gp, frequency=1000, duty_cycle=0):
        pwm = FakePWM(duty_cycle)
        pwm.frequency = frequency
        if int(gp) == 21:
            self.thr_pwm = pwm
        return self._claim(gp, pwm)

    now = 0.0

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def clock(self):
        return self.now


class _Fake:
    def deinit(self):
        self.bank.claimed.pop(self.gp, None)


class FakeOut(_Fake):
    def __init__(self, v):
        self._v = v

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        self._v = bool(v)
        self.bank.log.append((self.gp, self._v))


class FakeADC(_Fake):
    value = 1234


class FakeRCNode(_Fake):
    """GP28 readback: follows the GP21 duty with optional gain/offset/fault."""
    def __init__(self, bank):
        self._bank = bank
        self.gain, self.offset, self.stuck = 0.98, 300, None

    @property
    def value(self):
        if self.stuck is not None:
            return self.stuck
        return int(self.offset + self.gain * self._bank.thr_pwm.duty_cycle)


class FakePWM(_Fake):
    def __init__(self, d):
        self.duty_cycle = d


PINS = {   # same shape as mux_blend settings_example.json
    "sense": [0, 1, 2, 3],
    "adc_mux": {"common": 26, "a": 18, "b": 19, "c": 20, "inh": None,
                "laser_y": [0, 1, 2, 3, 4, 5, 6, 7]},
    "threshold_mux": {"a": 18, "b": 19, "c": 20, "inh": None,
                      "y": [0, 1, 2, 3, 4, 5, 6, 7], "pwm_gp": 21,
                      "sense_gp": 28},
}

NEW_STYLE = {
    "sense": [0, 1, 2, 3],
    "select_groups": {"tower": {"a": 18, "b": 19, "c": 20, "inh": 22,
                                "settle_us": 500}},
    "adc_mux": {"select": "tower", "common": 26, "y": [0, 1, 2, 3]},
    "threshold_mux": {"select": "tower", "pwm_gp": 21, "sense_gp": 28},
}

results = []


def case(fn):
    M.reset_registry()
    try:
        fn()
        results.append((fn.__name__, True, ""))
        print("  ok ", fn.__name__)
    except Exception as e:
        results.append((fn.__name__, False, repr(e)))
        print("  FAIL", fn.__name__, repr(e))


def expect(exc, f, *a):
    try:
        f(*a)
    except exc:
        return
    raise AssertionError("expected %s" % exc.__name__)


@case
def test_reproduce_current_bug():
    # What mux_blend does: two independent claims of A/B/C.
    bank = FakePinBank()
    bank.output(18); bank.output(19); bank.output(20)
    expect(ValueError, bank.output, 18)


@case
def test_legacy_settings_share_one_tower():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    assert len(bells) == 4
    assert all(b.tower is tower for b in bells)
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28], sorted(bank.claimed)


@case
def test_new_style_with_inh():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(NEW_STYLE, bank)
    assert 22 in bank.claimed and tower.settle_s == 0.0005
    with tower.select(bells[3]):
        assert tower.current == 3
    # INH went high before A/B/C changed and low after (break-before-make)
    seq = [g for g, _ in bank.log]
    assert seq[0] == 22 and seq[-1] == 22, bank.log


@case
def test_select_drives_lines_and_settles_once():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    with tower.select(bells[2]):
        assert (tower._a.value, tower._b.value, tower._c.value) == (False, True, False)
        assert bells[2].read_sense() == 1234
        bells[2].set_drive(40000)
        assert bells[2].drive.pwm.duty_cycle == 40000
        with tower.select(bells[2]):            # re-entry: no new switch
            pass
    assert bank.slept.count(tower.settle_s) == 1, bank.slept
    with tower.select(bells[2]):                 # already on ch2: no settle
        pass
    assert bank.slept.count(tower.settle_s) == 1


@case
def test_other_bell_while_held_raises():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    with tower.select(bells[0]):
        expect(M.ChannelBusy, tower.select(bells[1]).__enter__)
        assert tower.current == 0                # no silent switch
    with tower.select(bells[1]):                 # free again
        assert tower.current == 1


@case
def test_use_without_selection_raises():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    expect(M.NotSelected, bells[0].read_sense)
    with tower.select(bells[1]):
        expect(M.NotSelected, bells[0].read_sense)   # wrong bell
        expect(M.NotSelected, bells[0].set_drive, 1)


@case
def test_exception_inside_with_releases():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    try:
        with tower.select(bells[0]):
            raise KeyError("boom")
    except KeyError:
        pass
    assert tower.holder is None


@case
def test_mismatched_y_rejected():
    bank = FakePinBank()
    bad = dict(PINS)
    bad["threshold_mux"] = dict(PINS["threshold_mux"], y=[1, 0, 2, 3])
    expect(M.MuxConfigError, M.build_from_pins, bad, bank)


@case
def test_different_abc_rejected():
    bank = FakePinBank()
    bad = dict(PINS)
    bad["threshold_mux"] = dict(PINS["threshold_mux"], a=17)
    expect(M.MuxConfigError, M.build_from_pins, bad, bank)


@case
def test_registry_returns_same_tower():
    bank = FakePinBank()
    spec = {"a": 18, "b": 19, "c": 20}
    t1 = M.get_tower("tower", spec, bank)
    t2 = M.get_tower("tower", spec, bank)
    assert t1 is t2
    expect(M.MuxConfigError, M.get_tower, "tower", {"a": 1, "b": 2, "c": 3}, bank)


@case
def test_deinit_releases_and_refuses_while_held():
    bank = FakePinBank()
    tower = M.get_tower("tower", {"a": 18, "b": 19, "c": 20}, bank)
    with tower.select(4):
        expect(M.ChannelBusy, tower.deinit)
    tower.deinit()
    assert not bank.claimed
    expect(RuntimeError, tower.select(1).__enter__)
    # can be rebuilt after deinit (soft restart / rescan)
    M.get_tower("tower", {"a": 18, "b": 19, "c": 20}, bank)


@case
def test_async_waits_instead_of_switching():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    order = []

    async def holder():
        async with tower.aselect(bells[0]):
            order.append(("hold0", tower.current))
            await asyncio.sleep(0.03)
            order.append(("still0", tower.current))

    async def waiter():
        await asyncio.sleep(0.005)
        async with tower.aselect(bells[1]):
            order.append(("got1", tower.current))

    async def main():
        await asyncio.gather(holder(), waiter())
    asyncio.run(main())
    assert order == [("hold0", 0), ("still0", 0), ("got1", 1)], order


@case
def test_async_timeout():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)

    async def main():
        with tower.select(bells[0]):
            try:
                async with tower.aselect(bells[1], timeout_s=0.01):
                    pass
            except M.ChannelBusy:
                return True
        return False
    assert asyncio.run(main())


@case
def test_rc_tau_and_settle_numbers():
    tau = M.rc2_tau_s(10e3, 100e-9, 10e3, 1e-6)
    assert abs(tau - 0.0205) < 0.0005, tau
    full = M.rc_settle_s(tau, 1.0, 1 / 4096.0)
    assert 0.16 < full < 0.18, full            # sim: 171 ms to 1/4096
    assert M.rc_settle_s(tau, 0.0001, 1 / 4096.0) == 0.0


@case
def test_drive_waits_rc_and_verifies_readback():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    d = bells[0].drive
    assert bank.thr_pwm.frequency == 20000
    with tower.select(bells[0]):
        got = bells[0].set_drive(32768)
        assert abs(got - 32768) < 1024, got
        half = d.last_settle_s
        assert 0.11 < half < 0.16, half         # 50 % step -> ~157 ms
        bells[0].set_drive(32768)               # no change -> no wait
        assert d.last_settle_s == 0.0
    assert any(abs(x - half) < 1e-9 for x in bank.slept)


@case
def test_readback_fault_raises():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    bank.claimed[28].stuck = 0                  # open RC / dead PWM
    with tower.select(bells[1]):
        expect(M.ThresholdMismatch, bells[1].set_drive, 40000)


@case
def test_calibrate_absorbs_gain_offset():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    node = bank.claimed[28]
    node.gain, node.offset = 0.9, 1500          # outside default tol at top
    d = bells[2].drive
    with tower.select(bells[2]):
        expect(M.ThresholdMismatch, bells[2].set_drive, 60000)
        g, o = d.calibrate(bells[2].y)
        assert abs(g - 0.9) < 0.01 and abs(o - 1500) < 50, (g, o)
        bells[2].set_drive(60000)


@case
def test_drive_needs_selection():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    expect(M.NotSelected, bells[0].set_drive, 100)


@case
def test_laser_presence_bands():
    k = M.COUNTS_PER_MA
    assert abs(k - 297.9) < 0.5, k
    # mentor: ~5 mA module -> ~75 mV -> ~1490 counts (16-bit scale)
    assert abs(5 * k - 1490) < 5
    assert M.classify_laser(700, 700 + 1490)[0] == "ok"
    assert M.classify_laser(700, 700 + 400)[0] == "open"        # <2 mA
    assert M.classify_laser(700, 700 + int(70 * k))[0] == "over"
    assert M.classify_laser(2500, 4000)[0] == "stuck_on"
    st, i_on, _ = M.classify_laser(700, 9100)                    # README bench
    assert st == "ok" and 27 < i_on < 29
    assert M.classify_laser(700, 700 + int(15 * k), ref_ma=28.0)[0] == "drift"


@case
def test_measure_laser_uses_selection_and_leaves_off():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    laser = bank.pwm_out(8)
    class CurrentADC:
        @property
        def value(self):
            assert tower.is_held_by(bells[0].y)
            return 700 + (1490 if laser.duty_cycle == 65535 else 0)
    dark, on = M.measure_laser(tower, bells[0], laser, CurrentADC())
    assert (dark, on) == (700, 2190) and laser.duty_cycle == 0
    assert tower.holder is None


# ---- loopback plant simulation -------------------------------------------
import random


class Plant:
    """Per-channel plugs. A: laser GPn -> sense. B: Orange -> Green/White."""
    def __init__(self, bank, tower, plug_a, plug_b, ron_ratio=0.99,
                 broken_b=(), dac_gain=0.98, dac_off=200):
        self.bank, self.tower = bank, tower
        self.plug_a, self.plug_b, self.broken_b = plug_a, plug_b, set(broken_b)
        self.ron_ratio, self.g, self.o = ron_ratio, dac_gain, dac_off
        self.lasers = {}

    def duty(self):
        return self.bank.thr_pwm.duty_cycle

    def looped(self):
        ch = self.tower.current
        return ch in self.plug_b and ch not in self.broken_b

    def gp28(self):
        load = 0.835 if self.looped() else 1.0
        return int(self.o + self.g * self.duty() * load)

    def gp26(self):
        if self.looped():
            return int(self.gp28() * self.ron_ratio)
        return 700


class PlantADC:
    def __init__(self, fn):
        self.fn = fn

    @property
    def value(self):
        return self.fn()

    def deinit(self):
        pass


class SensePin:
    def __init__(self, plant, ch):
        self.plant, self.ch = plant, ch

    @property
    def value(self):
        if self.ch not in self.plant.plug_a:
            return False                       # head at rest / pulled low
        d = self.plant.lasers[self.ch].duty_cycle
        if d in (0, 65535):
            return d == 65535
        return random.random() < d / 65535.0


def _rig(plug_a, plug_b, **kw):
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    plant = Plant(bank, tower, plug_a, plug_b, **kw)
    bells[0].drive.back = PlantADC(plant.gp28)
    bells[0].sense.adc = PlantADC(plant.gp26)
    for b in bells:
        b.laser = bank.pwm_out(8 + b.index)
        plant.lasers[b.index] = b.laser
        b.sense_pin = SensePin(plant, b.index)
    return tower, bells, plant


@case
def test_loopback_threshold_path_classifies():
    random.seed(1)
    tower, bells, _ = _rig(plug_a={0, 1}, plug_b={0, 1})
    st = M.LoopbackSelfTest(tower)
    r0 = st.threshold_loopback(bells[0])
    assert r0["ok"] and r0["plug"] is True, r0
    r2 = st.threshold_loopback(bells[2])
    assert r2["ok"] and r2["plug"] is False, r2
    assert tower.holder is None and bells[0].drive.duty == 0


@case
def test_loopback_broken_switch_is_fault():
    tower, bells, plant = _rig(plug_a={1}, plug_b={1}, ron_ratio=0.6)
    r = M.LoopbackSelfTest(tower).threshold_loopback(bells[1])
    assert not r["ok"] and r["plug"] is None, r


@case
def test_loopback_digital_and_sweep():
    random.seed(2)
    tower, bells, _ = _rig(plug_a={0}, plug_b={0})
    st = M.LoopbackSelfTest(tower)
    d = st.digital_loopback(bells[0])
    assert d["ok"] and d["seen"] == [False, True, False], d
    s = st.duty_sweep(bells[0])
    assert s["ok"] and s["monotonic"], s
    n = st.digital_loopback(bells[3])           # no plug A on ch3
    assert n["plug"] is False and not n["ok"]


@case
def test_dac_linearity_on_unloaded_channel():
    tower, bells, _ = _rig(plug_a=set(), plug_b={0})
    r = M.LoopbackSelfTest(tower).dac_linearity(bells[2])
    assert r["ok"] and abs(r["gain"] - 0.98) < 0.01, r


@case
def test_full_run_report():
    random.seed(3)
    tower, bells, _ = _rig(plug_a={0, 1}, plug_b={0, 1})
    rep = M.LoopbackSelfTest(tower).run(bells)
    assert rep["ok"], rep
    assert rep["channels"][0]["threshold"]["plug"] is True
    assert "digital" not in rep["channels"][3]
    assert rep["dac"]["ch"] == 2


# ---- socket classification ------------------------------------------------
BASE = dict(l0=0.0, l50=1.0, r50=0.02, dark=700, di_ma=0.0, x=0.0,
            follow=False, pull_follow=True, thr_toggle=False)


def sig(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


@case
def test_classify_signatures():
    C = M.classify_socket
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
        sig(l50=0.835, r50=0.99),                     # strap A open
        sig(follow=True),                             # strap B open
        sig(l50=0.33, pull_follow=False),             # Orange<->Blue/White swap
        sig(di_ma=5.0),                               # sense conductor open
        sig(pull_follow=False),                       # pin 7 / pin 6 open
        sig(thr_toggle=True),                         # new head, no laser
    ]
    for f in faults:
        k, why = C(f)
        assert k == "fault", (f, k, why)


class PullSensePin(SensePin):
    pull = "down"

    @property
    def value(self):
        if self.ch not in self.plant.plug_a:
            return self.pull == "up"
        return SensePin.value.fget(self)


@case
def test_survey_loopback_and_empty_end_to_end():
    random.seed(4)
    tower, bells, plant = _rig(plug_a={0}, plug_b={0})
    for b in bells:
        b.sense_pin = PullSensePin(plant, b.index)
    sv = M.SocketSurvey(tower, pull_up="up", pull_down="down")
    r0 = sv.classify(bells[0])
    assert r0["kind"] == "loopback", r0
    r3 = sv.classify(bells[3])
    assert r3["kind"] == "empty", r3
    assert tower.holder is None


@case
def test_ethernet_magnetics_signature_is_fault():
    k, why = M.classify_socket(sig(di_ma=200.0, pull_follow=False))
    assert k == "fault" and "magnetics" in why[0], (k, why)


@case
def test_channel_gate():
    g = M.ChannelGate(4)
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


@case
def test_gp27_never_claimed_and_warned():
    bank = FakePinBank()
    legacy = dict(PINS)
    legacy["threshold_mux"] = dict(PINS["threshold_mux"], common=27)
    tower, _ = M.build_from_pins(legacy, bank)
    assert 27 not in bank.claimed and tower.warnings, tower.warnings


@case
def test_sample_hold_numbers():
    assert abs(M.SHARE_FRAC - 0.0909) < 1e-3
    assert abs(M.refresh_interval_s() - 0.020) < 1e-9       # 5 mV, 25 nA
    assert abs(M.refresh_interval_s(bias_a=250e-9) - 0.002) < 1e-9


@case
def test_refresher_round_robin_same_target():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    gate = M.ChannelGate(4)
    for i in (0, 1, 3):
        gate.set_kind(i, "head_new")
    gate.set_kind(2, "fault")
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, gate=gate,
                             clock=bank.clock)
    for b in bells:
        r.set_target(b, 20000)
    seen = [r.step() for _ in range(6)]
    assert seen == [0, 1, 3, 0, 1, 3], seen            # fault ch skipped
    assert abs(r.cycle_s() - 0.003) < 1e-9
    assert r.stale() == []                             # all refreshed < 20 ms
    bank.now += 0.05
    assert sorted(r.stale()) == [0, 1, 3]
    assert tower.holder is None


@case
def test_refresher_skips_busy_tower():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.set_target(bells[1], 1000)
    with tower.select(bells[0]):                       # e.g. a laser probe
        assert r.step() is None and r.skipped_busy == 1
        assert tower.current == 0
    assert r.step() == 1


@case
def test_refresher_retarget_settles_rc():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.set_target(bells[0], 40000)
    t0 = bank.now
    r.step()
    assert bank.now - t0 > 0.1                         # RC re-settle paid


@case
def test_droop_estimate_from_share_step():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    node = {"v": 30000.0}
    hold = {1: 30000.0 + 1100}                         # ch1 drooped up 1100

    class Node:
        @property
        def value(self):
            ch = tower.current
            if ch in hold and tower.holder == ch:
                return int(node["v"] + M.SHARE_FRAC * (hold[ch] - node["v"]))
            return int(node["v"])
    bells[0].drive.back = Node()
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    with tower.select(bells[0]):
        pass
    est = r.droop_estimate(bells[1])
    assert abs(est - 1100) < 15, est


@case
def test_no_board_state_and_gate():
    k = {0: "loopback", 1: "head_new", 2: "empty", 3: "fault",
         4: "empty", 5: "empty", 6: "empty", 7: "empty"}
    auto = [dict(M.DEFAULT_BOARDS[0]), dict(M.DEFAULT_BOARDS[1], present=None)]
    out = M.apply_boards(k, auto)
    assert [out[c] for c in range(4, 8)] == ["no_board"] * 4
    assert out[2] == "empty" and out[3] == "fault"
    k2 = dict(k); k2[5] = "head_new"                   # board 2 fitted, one head
    out2 = M.apply_boards(k2, auto)
    assert out2[4] == "empty" and out2[5] == "head_new"
    out3 = M.apply_boards(k2)                          # default: conn2 absent
    assert out3[5] == "no_board"
    g = M.ChannelGate(8)
    for c, v in out.items():
        g.set_kind(c, v)
    assert not any(g.may_drive(c) for c in range(4, 8))


@case
def test_survey_all_never_probes_absent_board():
    random.seed(5)
    bank = FakePinBank()
    pins8 = dict(PINS, sense=list(range(8)))
    tower, bells = M.build_from_pins(pins8, bank)
    plant = Plant(bank, tower, plug_a={0}, plug_b={0})
    bells[0].drive.back = PlantADC(plant.gp28)
    bells[0].sense.adc = PlantADC(plant.gp26)
    for b in bells:
        b.laser = FakePWM(0)
        plant.lasers[b.index] = b.laser
        b.sense_pin = PullSensePin(plant, b.index)
    sv = M.SocketSurvey(tower, pull_up="up", pull_down="down")
    g = M.ChannelGate(8)
    sel_before = tower.selects
    rep = M.survey_all(sv, bells, g)
    assert rep[0]["kind"] == "loopback" and rep[3]["kind"] == "empty"
    assert all(rep[c]["kind"] == "no_board" for c in range(4, 8))
    assert all(bells[c].laser.duty_cycle == 0 for c in range(8))
    assert g.may_drive(0) and not g.may_drive(5)


@case
def test_panel_pixels_layout():
    px = M.panel_pixels({0: "loopback", 1: "head_new", 2: "fault"},
                        link="ws_client", activity="ringing")
    assert len(px) == 6
    assert px[0] == M.LED_LINK["ws_client"] and px[5] == M.LED_ACTIVITY["ringing"]
    assert px[1] == M.LED_KIND["loopback"] and px[3] == M.LED_KIND["fault"]
    assert px[4] == M.LED_KIND["unknown"]


@case
def test_default_config_is_8_channels_1_to_4_populated():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(None, bank)
    assert len(bells) == 8 and [b.y for b in bells] == list(range(8))
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28], sorted(bank.claimed)
    assert M.POPULATED == [0, 1, 2, 3]
    assert bells[0].drive.pwm.frequency == 20000
    out = M.apply_boards({c: "empty" for c in range(8)},
                         M.DEFAULT_PINS["connector_boards"])
    assert [out[c] for c in range(8)] == ["empty"] * 4 + ["no_board"] * 4


import json
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


@case
def test_current_board_settings_legacy():
    cur = _load("settings_current.json")
    # what mux_blend does with it: Mux4051 x2 on the same a/b/c
    bank = FakePinBank()
    for gp in (26, 18, 19, 20):                       # claim_muxes: adc_mux
        bank.output(gp) if gp != 26 else bank.analog_in(gp)
    bank.analog_in(27)                                # thr Mux4051 common
    expect(ValueError, bank.output, 18)               # -> "GP18 in use"
    # the draft with the same file
    M.reset_registry()
    bank = FakePinBank()
    tower, bells = M.build_from_pins(cur, bank)
    assert len(bells) == 8 and [b.y for b in bells] == list(range(8))
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28], sorted(bank.claimed)
    assert 27 not in bank.claimed and any("27" in w for w in tower.warnings)


@case
def test_proposed_settings_build():
    prop = _load("settings_proposed.json")
    bank = FakePinBank()
    tower, bells = M.build_from_pins(prop, bank)
    assert len(bells) == 8 and not tower.warnings
    assert "fitted" not in prop["pins"]
    assert sorted(bank.claimed) == [18, 19, 20, 21, 26, 28]
    assert bells[0].drive.pwm.frequency == 20000
    assert abs(bells[0].drive.tau_s - 0.0205) < 0.0005
    out = M.apply_boards({c: "empty" for c in range(8)},
                         prop["pins"]["connector_boards"])
    assert [out[c] for c in range(4, 8)] == ["no_board"] * 4



# ---- RP2350 PWM slice check ------------------------------------------------
@case
def test_pwm_slice_formula():
    # lasers GP8..15 -> 4A 4B 5A 5B 6A 6B 7A 7B; threshold GP21 -> 2B
    got = [M.pwm_slice(g) for g in range(8, 16)]
    assert got == [(4, "A"), (4, "B"), (5, "A"), (5, "B"),
                   (6, "A"), (6, "B"), (7, "A"), (7, "B")], got
    assert M.pwm_slice(21) == (2, "B")
    assert M.pwm_slice(28) == (6, "A") == M.pwm_slice(12)
    assert M.pwm_slice(0) == (0, "A") and M.pwm_slice(16) == (0, "A")
    assert M.pwm_slice(29) == (6, "B")
    expect(M.PwmSliceError, M.pwm_slice, 30)       # not on RP2350A


@case
def test_pwm_gp12_gp28_clash_fails():
    try:
        M.check_pwm_slices([(12, 1000, "laser ch5"), (28, 20000, "threshold")])
    except M.PwmSliceError as e:
        assert "slice 6 channel A" in str(e), str(e)
    else:
        raise AssertionError("GP12 + GP28 must clash")
    # and through build_from_pins, before any pin is claimed
    bad = json.loads(json.dumps(_load("settings_proposed.json")))
    bad["pins"]["threshold_mux"]["pwm_gp"] = 28
    bank = FakePinBank()
    expect(M.PwmSliceError, M.build_from_pins, bad, bank)
    assert bank.claimed == {}, bank.claimed


@case
def test_pwm_gp21_with_lasers_passes():
    pins = [(g, 1000, "laser") for g in range(8, 16)] + [(21, 20000, "thr")]
    table, warn = M.check_pwm_slices(pins)
    assert warn == [] and table[21][:2] == (2, "B")
    assert sorted(table) == list(range(8, 16)) + [21]
    bank = FakePinBank()
    tower, _ = M.build_from_pins(_load("settings_proposed.json"), bank)
    assert tower.pwm_slices[21][:3] == (2, "B", 20000)
    assert tower.pwm_slices[12][:2] == (6, "A")


@case
def test_pwm_same_slice_other_channel_ok_same_freq():
    table, warn = M.check_pwm_slices({8: 1000, 9: 1000})     # 4A + 4B
    assert warn == []
    expect(M.PwmSliceError, M.check_pwm_slices, [8, 8])      # listed twice


@case
def test_pwm_shared_slice_freq_mismatch():
    # GP20 is 2A, GP21 is 2B: different channels, but one slice -> one freq
    expect(M.PwmSliceError, M.check_pwm_slices, {20: 1000, 21: 20000})
    _, warn = M.check_pwm_slices({20: 1000, 21: 20000}, freq_mismatch="warn")
    assert len(warn) == 1 and "slice 2" in warn[0], warn
    # through build_from_pins: a laser moved onto GP20 (2A, 1 kHz) would
    # share slice 2 with the 20 kHz threshold PWM on GP21 (2B)
    cfg = json.loads(json.dumps(_load("settings_proposed.json")))
    cfg["pins"]["laser_pwm"] = [8, 9, 10, 11, 12, 13, 14, 20]   # 20 = 2A
    bank = FakePinBank()
    expect(M.PwmSliceError, M.build_from_pins, cfg, bank)
    M.reset_registry()
    bank = FakePinBank()
    tower, _ = M.build_from_pins(cfg, bank, freq_mismatch="warn")
    assert any("frequency" in w for w in tower.warnings), tower.warnings


# ---- fast refresh vs slow update -------------------------------------------
@case
def test_refresh_rewrites_target_slow_update_changes_it():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    r.update_targets(30000)                          # slow: same for all
    assert r.target_updates == 4
    for _ in range(8):                               # fast: two cycles
        r.step()
    assert r.target == {0: 30000, 1: 30000, 2: 30000, 3: 30000}
    assert r.target_updates == 4                     # refresh never retargets
    r.update_targets(30000)                          # unchanged: no update
    assert r.target_updates == 4
    r.update_targets({2: 31000})                     # slow drift on one ch
    assert r.target_updates == 5 and r.target[2] == 31000
    seen = {}
    for _ in range(4):
        i = r.step()
        seen[i] = bells[0].drive.duty
    assert seen[2] == 31000 and seen[3] == 30000, seen


@case
def test_refresher_measured_interval_overrides_datasheet():
    bank = FakePinBank()
    tower, bells = M.build_from_pins(PINS, bank)
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock)
    assert r.interval_source == "datasheet" and abs(r.interval_s - 0.02) < 1e-9
    r = M.ThresholdRefresher(tower, bells[0].drive, bells, clock=bank.clock,
                             interval_s=0.5)
    assert r.interval_source == "measured" and r.interval_s == 0.5
    assert abs(r.datasheet_interval_s - 0.02) < 1e-9
    prop = _load("settings_proposed.json")["pins"]["threshold_mux"]
    r = M.refresher_from_cfg(tower, bells, prop, clock=bank.clock)
    assert r.dwell_s == 0.001
    exp = prop["refresh"].get("interval_s")
    assert r.interval_source == ("datasheet" if exp is None else "measured")


@case
def test_proposed_settings_single_threshold_pwm_hz():
    prop = _load("settings_proposed.json")
    thr = prop["pins"]["threshold_mux"]
    assert thr["pwm_hz"] == 20000 and thr["pwm_gp"] == 21
    flat = json.dumps(prop)
    assert flat.count('"pwm_hz"') == 1, "one threshold PWM frequency key"

failed = [r for r in results if not r[1]]
print("\n%d passed, %d failed" % (len(results) - len(failed), len(failed)))
sys.exit(1 if failed else 0)
