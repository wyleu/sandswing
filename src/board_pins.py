# board_pins.py -- the only place that turns a GP number into a pin object.
# Real hardware. Tests pass their own factory with the same four methods.
import time


def _gp(gp):
    import board
    return getattr(board, "GP%d" % int(gp))


class BoardPins:
    def output(self, gp, value=False):
        import digitalio
        p = digitalio.DigitalInOut(_gp(gp))
        p.switch_to_output(value=value)
        return p

    def input(self, gp, pull_up=True):
        import digitalio
        p = digitalio.DigitalInOut(_gp(gp))
        p.direction = digitalio.Direction.INPUT
        p.pull = digitalio.Pull.UP if pull_up else None
        return p

    def analog_in(self, gp):
        import analogio
        return analogio.AnalogIn(_gp(gp))

    def pwm_out(self, gp, frequency=1000, duty_cycle=0):
        import pwmio
        return pwmio.PWMOut(_gp(gp), frequency=frequency, duty_cycle=duty_cycle)

    def sleep(self, s):
        time.sleep(s)


def release(objs):
    """deinit every object, ignoring errors (duty to 0 first for PWM)."""
    for o in objs:
        if o is None:
            continue
        try:
            if hasattr(o, "duty_cycle"):
                o.duty_cycle = 0
            o.deinit()
        except Exception:
            pass
