# the note left in nvm before our own resets (watchdog_guard.mark_reset / boot_reasons)
from support import case, finish
import watchdog_guard as wg


class Nvm(bytearray):
    pass


def fresh():
    return Nvm(4096)


@case
def test_mark_then_take_names_the_reset_and_clears_it():
    n = fresh()
    wg.mark_reset(b"SRST", nvm=n)
    assert wg.take_mark(nvm=n) == "SELF-RESET"
    assert wg.take_mark(nvm=n) is None


@case
def test_crash_mark():
    n = fresh()
    wg.mark_reset(b"CRSH", nvm=n)
    assert wg.take_mark(nvm=n) == "CRASH-RESET"


@case
def test_no_mark_means_a_real_watchdog_stays_a_watchdog():
    assert wg.take_mark(nvm=fresh()) is None


@case
def test_unknown_bytes_are_ignored_and_cleared():
    n = fresh()
    n[-4:] = b"abcd"
    assert wg.take_mark(nvm=n) is None
    assert bytes(n[-4:]) == b"\0\0\0\0"


@case
def test_boot_reasons_prefers_the_mark_over_watchdog():
    import sys
    import types
    n = fresh()
    wg.mark_reset(b"SRST", nvm=n)
    old = sys.modules.get("microcontroller")
    class Cpu:
        reset_reason = "microcontroller.ResetReason.WATCHDOG"
    fake = types.ModuleType("microcontroller")
    fake.cpu = Cpu()
    sys.modules["microcontroller"] = fake
    try:
        assert wg.boot_reasons(nvm=n)[0] == "SELF-RESET"
        assert wg.boot_reasons(nvm=n)[0] == "WATCHDOG"       # note was used up
        wg.mark_reset(b"SRST", nvm=n)
        Cpu.reset_reason = "microcontroller.ResetReason.POWER_ON"
        assert wg.boot_reasons(nvm=n)[0] == "POWER_ON"       # a power cycle after the note
    finally:
        if old is None:
            del sys.modules["microcontroller"]
        else:
            sys.modules["microcontroller"] = old


@case
def test_no_nvm_never_raises():
    class Bad:
        def __len__(self):
            raise OSError("no nvm")
    wg.mark_reset(b"SRST", nvm=Bad())
    assert wg.take_mark(nvm=Bad()) is None


finish()
