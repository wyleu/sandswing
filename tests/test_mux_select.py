# tests for mux_select.py (split from test_mux_select_draft.py)
from support import *   # fakes, rigs, case/expect, finish
import mux_errors
import mux_select
import tower_build


@case
def test_reproduce_current_bug():
    # What mux_blend does: two independent claims of A/B/C.
    bank = FakePinBank()
    bank.output(18); bank.output(19); bank.output(20)
    expect(ValueError, bank.output, 18)


@case
def test_select_drives_lines_and_settles_once():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
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
    tower, bells = tower_build.build_from_pins(PINS, bank)
    with tower.select(bells[0]):
        expect(mux_errors.ChannelBusy, tower.select(bells[1]).__enter__)
        assert tower.current == 0                # no silent switch
    with tower.select(bells[1]):                 # free again
        assert tower.current == 1


@case
def test_use_without_selection_raises():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    expect(mux_errors.NotSelected, bells[0].read_sense)
    with tower.select(bells[1]):
        expect(mux_errors.NotSelected, bells[0].read_sense)   # wrong bell
        expect(mux_errors.NotSelected, bells[0].set_drive, 1)


@case
def test_exception_inside_with_releases():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
    try:
        with tower.select(bells[0]):
            raise KeyError("boom")
    except KeyError:
        pass
    assert tower.holder is None


@case
def test_registry_returns_same_tower():
    bank = FakePinBank()
    spec = {"a": 18, "b": 19, "c": 20}
    t1 = mux_select.get_tower("tower", spec, bank)
    t2 = mux_select.get_tower("tower", spec, bank)
    assert t1 is t2
    expect(mux_errors.MuxConfigError, mux_select.get_tower, "tower", {"a": 1, "b": 2, "c": 3}, bank)


@case
def test_deinit_releases_and_refuses_while_held():
    bank = FakePinBank()
    tower = mux_select.get_tower("tower", {"a": 18, "b": 19, "c": 20}, bank)
    with tower.select(4):
        expect(mux_errors.ChannelBusy, tower.deinit)
    tower.deinit()
    assert not bank.claimed
    expect(RuntimeError, tower.select(1).__enter__)
    # can be rebuilt after deinit (soft restart / rescan)
    mux_select.get_tower("tower", {"a": 18, "b": 19, "c": 20}, bank)


@case
def test_async_waits_instead_of_switching():
    bank = FakePinBank()
    tower, bells = tower_build.build_from_pins(PINS, bank)
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
    tower, bells = tower_build.build_from_pins(PINS, bank)

    async def main():
        with tower.select(bells[0]):
            try:
                async with tower.aselect(bells[1], timeout_s=0.01):
                    pass
            except mux_errors.ChannelBusy:
                return True
        return False
    assert asyncio.run(main())


finish()
