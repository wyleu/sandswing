# tests for socket_survey.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import channel_gate
import socket_survey
import tower_build


@case
def test_survey_loopback_and_empty_end_to_end():
    random.seed(4)
    tower, bells, plant = rig(plug_a={0}, plug_b={0})
    for b in bells:
        b.sense_pin = PullSensePin(plant, b.index)
    sv = socket_survey.SocketSurvey(tower, pull_up="up", pull_down="down")
    r0 = sv.classify(bells[0])
    assert r0["kind"] == "loopback", r0
    r3 = sv.classify(bells[3])
    assert r3["kind"] == "empty", r3
    assert tower.holder is None


@case
def test_survey_all_never_probes_absent_board():
    random.seed(5)
    bank = FakePinBank()
    pins8 = dict(PINS, sense=list(range(8)))
    tower, bells = tower_build.build_from_pins(pins8, bank)
    plant = Plant(bank, tower, plug_a={0}, plug_b={0})
    bells[0].drive.back = PlantADC(plant.gp28)
    bells[0].sense.adc = PlantADC(plant.gp26)
    for b in bells:
        b.laser = FakePWM(0)
        plant.lasers[b.index] = b.laser
        b.sense_pin = PullSensePin(plant, b.index)
    sv = socket_survey.SocketSurvey(tower, pull_up="up", pull_down="down")
    g = channel_gate.ChannelGate(8)
    sel_before = tower.selects
    rep = socket_survey.survey_all(sv, bells, g)
    assert rep[0]["kind"] == "loopback" and rep[3]["kind"] == "empty"
    assert all(rep[c]["kind"] == "no_board" for c in range(4, 8))
    assert all(bells[c].laser.duty_cycle == 0 for c in range(8))
    assert g.may_drive(0) and not g.may_drive(5)


finish()
