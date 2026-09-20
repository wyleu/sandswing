# sand_optical.py
#
# Shared optical drive + digital PT sense (Sand* farm)
# ----------------------------------------------------
# Filename: sand_optical.py
#
# Overview
# --------
# Thin helpers for laser (or LED) PWM and phototransistor digital inputs.
# Used by optical path tests and later by live Sandswing sensing.
#
# Pin map (typical Sandswing, 2 heads)
# ------------------------------------
#   laser PWM  = output_base + ch  (GP0, GP1)
#   PT digital = input_base  + ch  (GP5, GP6)
#
# hardware_config often claims output pins as DigitalInOut (leds[]).
# This module deinit's those and opens PWMOut for level control.
#
# Typical use
# -----------
#   from sand_optical import OpticalHeads
#   heads = OpticalHeads.from_hw(hw, num=2)
#   heads.laser_duty(0, 40000)
#   raw = heads.read_pt(0)
#   heads.all_off()

import board
import pwmio

class OpticalHeads:
    def __init__(self, laser_pwms, inputs, laser_gps, input_base, invert_pt=True):
        self.laser_pwms = laser_pwms
        self.inputs = inputs
        self.laser_gps = laser_gps
        self.input_base = input_base
        self.invert_pt = invert_pt
        self.num = len(laser_pwms)

    @classmethod
    def from_hw(cls, hw, num=None, invert_pt=True, pwm_hz=1000):
        """
        hw = create_hardware(cfg) result.
        Releases leds[0..num) and creates PWMOut on the same GPs.
        """
        pins = hw["pins"]
        inputs = hw["inputs"]
        leds = hw["leds"]
        n = num if num is not None else min(len(inputs), len(leds))
        n = min(n, len(inputs), len(leds))

        laser_gps = [pins["output_base"] + i for i in range(n)]
        for i in range(n):
            try:
                leds[i].deinit()
            except Exception:
                pass

        laser_pwms = []
        for gp in laser_gps:
            laser_pwms.append(
                pwmio.PWMOut(
                    getattr(board, "GP%d" % gp),
                    frequency=pwm_hz,
                    duty_cycle=0,
                )
            )

        print(
            "optical heads: %d  laser GP%s  PT base GP%d"
            % (n, laser_gps, pins["input_base"])
        )
        return cls(
            laser_pwms,
            inputs[:n],
            laser_gps,
            pins["input_base"],
            invert_pt=invert_pt,
        )

    def laser_duty(self, ch, duty):
        """Set one head duty 0..65535; other heads off."""
        duty = max(0, min(65535, int(duty)))
        for o in range(self.num):
            self.laser_pwms[o].duty_cycle = duty if o == ch else 0
            
    def lasers_duties(self, duties):
        for i, p in enumerate(self.laser_pwms):
            d = duties[i] if i < len(duties) else 0
            p.duty_cycle = max(0, min(65535, int(d)))
        
    def laser_on(self, ch, on, duty=40000):
        """Fixed-level on/off for path test."""
        self.laser_duty(ch, duty if on else 0)

    def all_off(self):
        for p in self.laser_pwms:
            p.duty_cycle = 0
            
    def lasers_all_duty(self, duty):
        """Steady level on every head (live / basic detect)."""
        duty = max(0, min(65535, int(duty)))
        for p in self.laser_pwms:
            p.duty_cycle = duty

    def read_pt(self, ch):
        """Digital phototransistor. invert_pt=True → not inputs[ch].value."""
        v = self.inputs[ch].value
        return (not v) if self.invert_pt else bool(v)

    def describe(self, ch):
        return "laser GP%d PT GP%d" % (
            self.laser_gps[ch],
            self.input_base + ch,
        )