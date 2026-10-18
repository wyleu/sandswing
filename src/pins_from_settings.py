"""
pins_from_settings.py
=====================
Pin map for the concentrator, from settings.json -> "pins".

All GP numbers come from settings.json as explicit lists. No arithmetic
(that is how GP numbers once drifted away from the loom).

This file only READS settings.  It claims nothing: the select lines and
both 4051 paths are claimed by tower_build / mux_select, the sense inputs
and lasers by concentrator.py.  (claim_lineup_hardware, claim_muxes and
claim_threshold are retired; see RETIRE.md.)

    "pins": {
      "sense": [0..7], "laser_pwm": [8..15], "laser_pwm_hz": 1000,
      "neopixel": 16,
      "adc_mux": {"common": 26, "a": 18, "b": 19, "c": 20, "y": [0..7]},
      "threshold_mux": {"a": 18, "b": 19, "c": 20, "y": [0..7],
                        "pwm_gp": 21, "pwm_hz": 20000, "sense_gp": 28}
    }

threshold_mux.common is ignored (the output 4051 COMMON is driven by the
filtered GP21 PWM and monitored by GP28; GP27 is unused).  There is no
"fitted" list: heads are found by the survey.
"""


def _raw_pins(cfg):
    raw = {}
    if hasattr(cfg, "get"):
        try:
            raw = cfg.get("pins") or {}
        except Exception:
            raw = {}
    if not raw and hasattr(cfg, "pins"):
        raw = cfg.pins or {}
    if not raw:
        data = getattr(cfg, "data", None) or getattr(cfg, "_data", None) or {}
        raw = data.get("pins", {}) if isinstance(data, dict) else {}
    return raw


def load_pinmap(cfg):
    """Returns dict: sense_gp, laser_gp, laser_hz, neopixel_gp, raw (the
    whole "pins" block, for tower_build)."""
    raw = _raw_pins(cfg)
    sense = list(raw.get("sense") or [0, 1, 2, 3])
    laser = list(raw.get("laser_pwm") or raw.get("drive") or [8, 9, 10, 11])
    if len(sense) != len(laser):
        raise ValueError("pins.sense and pins.laser_pwm must be the same length")
    thr = raw.get("threshold_mux") or {}
    if thr.get("common") is not None:
        print("threshold_mux.common GP%s ignored (not a mux pin)" % thr.get("common"))
    return {
        "sense_gp": [int(x) for x in sense],
        "laser_gp": [int(x) for x in laser],
        "laser_hz": int(raw.get("laser_pwm_hz", 1000)),
        "neopixel_gp": int(raw.get("neopixel", 16)),
        "raw": raw,
    }
