# bell.py — one optical channel (laser PWM + digital PT + 4051 analog)
#
# CircuitPython. Used by sandswing lineup; 8 slots, only "fitted" claim pins.
#
#   from pins_from_settings import load_pinmap
#   from mux4051 import Mux4051
#   from bell import Farm
#
#   pinmap = load_pinmap(cfg)
#   farm = Farm.from_pinmap(pinmap)
#   for bell in farm.fitted():
#       bell.laser(duty)
#       I = bell.analog()
#       pt = bell.pt()

import board
import digitalio
import pwmio

from mux4051 import Mux4051


def _gp(n):
    return getattr(board, "GP%d" % int(n))


class Bell:
    def __init__(self, index, laser_gp, sense_gp, analog_y, mux, fitted=True):
        self.index = int(index)
        self.analog_y = analog_y
        self.mux = mux
        self.fitted = bool(fitted) and laser_gp is not None
        self.laser_pwm = None
        self.sense = None
        if not self.fitted:
            return
        self.laser_pwm = pwmio.PWMOut(_gp(laser_gp), frequency=1000, duty_cycle=0)
        if sense_gp is not None:
            pin = digitalio.DigitalInOut(_gp(sense_gp))
            pin.switch_to_input(pull=digitalio.Pull.UP)
            self.sense = pin

    def laser(self, duty):
        if self.laser_pwm is None:
            return
        duty = max(0, min(65535, int(duty)))
        self.laser_pwm.duty_cycle = duty

    def laser_off(self):
        self.laser(0)

    def pt(self):
        """Active-low photodiode: True = beam seen."""
        if self.sense is None:
            return False
        return not self.sense.value

    def analog(self):
        if self.mux is None or self.analog_y is None:
            return None
        return self.mux.read(self.analog_y)

    def deinit(self):
        self.laser_off()
        if self.laser_pwm is not None:
            try:
                self.laser_pwm.deinit()
            except Exception:
                pass
            self.laser_pwm = None
        if self.sense is not None:
            try:
                self.sense.deinit()
            except Exception:
                pass
            self.sense = None


class Farm:
    def __init__(self, mux, bells):
        self.mux = mux
        self.bells = list(bells)

    def fitted(self):
        return [b for b in self.bells if b.fitted]

    def by_index(self, i):
        return self.bells[int(i)]

    def all_lasers_off(self):
        for b in self.bells:
            b.laser_off()

    def deinit(self):
        self.all_lasers_off()
        for b in self.bells:
            b.deinit()

    @classmethod
    def from_pinmap(cls, pinmap):
        mux_cfg = pinmap.get("adc_mux") or {}
        mux = Mux4051(
            common=mux_cfg["common"],
            a=mux_cfg["a"],
            b=mux_cfg["b"],
            c=mux_cfg["c"],
            inh=mux_cfg.get("inh"),
        )
        sense = list(pinmap.get("sense") or [])
        laser = list(pinmap.get("laser_pwm") or [])
        ys = list((mux_cfg.get("laser_y") or list(range(8))))
        fitted = set(int(x) for x in (pinmap.get("fitted") or []))
        n = max(len(sense), len(laser), len(ys), 8)
        while len(sense) < n:
            sense.append(None)
        while len(laser) < n:
            laser.append(None)
        while len(ys) < n:
            ys.append(None)
        bells = []
        for i in range(n):
            on = i in fitted
            bells.append(
                Bell(
                    index=i,
                    laser_gp=laser[i] if on else None,
                    sense_gp=sense[i] if on else None,
                    analog_y=ys[i],
                    mux=mux,
                    fitted=on,
                )
            )
        return cls(mux, bells)
