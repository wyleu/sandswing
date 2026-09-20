# hardware_config.py
#
# IR Bell Detector hardware factory (Sand* farm)
# -----------------------------------------------
# Filename: hardware_config.py
#
# Overview
# --------
# Creates the physical objects used by the IR bell-scan programs:
#
#   • Active-low digital inputs (IR receivers / switches)
#   • Matching GPIO LED outputs (compatibility; often unused with NeoPixels)
#   • IR carrier PWM (typically 38 kHz)
#   • TX LED and status LED
#   • Resolved pin map, IR parameters, and detection parameters
#
# Channel count is taken from detection.num_bells so only the pins actually
# required are claimed (avoids hard-coded range(8) clashes).
#
# NeoPixel data pin is exposed in the pins dict so the main program can build
# the strip without hard-coding a GPIO number.
#
# Configuration
# -------------
# Does NOT parse settings.json itself. Always uses config_loader:
#
#   from config_loader import load_config
#   from hardware_config import create_hardware
#
#   cfg = load_config()
#   hw = create_hardware(cfg)     # Config object or raw dict both OK
#
# Relevant settings.json sections:
#
#   "pins": {
#     "input_base": 5,
#     "output_base": 0,
#     "ir_led": 13,
#     "tx_led": 12,
#     "status_led": 22,
#     "neopixel": 15
#   },
#   "ir": {
#     "carrier_freq": 38000,
#     "duty_33": 21845
#   },
#   "detection": {
#     "num_bells": 4,
#     "mode": "latch_detect_with_middle",
#     "latch_timeout_ms": 100,
#     "min_middle_ms": 10,
#     "max_middle_ms": 80,
#     "scan_interval_ms": 10
#   }
#
# Device family
# -------------
# Intended for device.family of "sandbells" (and transitional "sandswing"
# while that board still runs bell software). For sandsense optical sensing
# use a future hardware_profile module (ADS / encoder lasers), not this file.
#
# Relationship to other modules
# -----------------------------
#   config_loader.py     – sole settings.json reader
#   hardware_config.py   – THIS FILE (bell IR GPIO / PWM)
#   code.py              – launcher only; does not call create_hardware
#   bell scan programs   – call create_hardware() after load_config()
#
# Return value of create_hardware()
# ---------------------------------
#   {
#     "inputs": [...],      # DigitalInOut list, length num_bells
#     "leds": [...],        # DigitalInOut list (True = OFF, inverted)
#     "ir_pwm": PWMOut,
#     "tx_led": DigitalInOut,
#     "status_led": DigitalInOut,
#     "pins": {...},
#     "ir": {...},
#     "detection": {...},
#   }
#
# Typical usage
# -------------
#   from config_loader import load_config
#   from hardware_config import create_hardware
#
#   cfg = load_config()
#   hw = create_hardware(cfg)
#   inputs = hw["inputs"]
#   det = hw["detection"]

import board
import digitalio
import pwmio

from config_loader import load_config

# Fallback if a key is missing after config_loader merge (historical map).
_PIN_DEFAULTS = {
    "input_base": 14,
    "output_base": 0,
    "ir_led": 13,
    "tx_led": 12,
    "status_led": 22,
    "neopixel": 15,
}
_IR_DEFAULTS = {
    "carrier_freq": 38000,
    "duty_33": 21845,
}
_DET_DEFAULTS = {
    "num_bells": 8,
    "mode": "latch_detect_with_middle",
    "latch_timeout_ms": 100,
    "min_middle_ms": 10,
    "max_middle_ms": 80,
    "scan_interval_ms": 10,
}


def _as_dict(cfg):
    """Accept Config from config_loader, raw dict, or None."""
    if cfg is None:
        return load_config().raw
    if hasattr(cfg, "raw"):
        return cfg.raw
    return cfg


def get_pins(cfg=None):
    """Resolved pin numbers for the bell IR path."""
    data = _as_dict(cfg)
    pins = data.get("pins") or {}
    return {
        "input_base": pins.get("input_base", _PIN_DEFAULTS["input_base"]),
        "output_base": pins.get("output_base", _PIN_DEFAULTS["output_base"]),
        "ir_led": pins.get("ir_led", _PIN_DEFAULTS["ir_led"]),
        "tx_led": pins.get("tx_led", _PIN_DEFAULTS["tx_led"]),
        "status_led": pins.get("status_led", _PIN_DEFAULTS["status_led"]),
        "neopixel": pins.get("neopixel", _PIN_DEFAULTS["neopixel"]),
    }


def get_ir(cfg=None):
    """IR carrier frequency and duty cycle."""
    data = _as_dict(cfg)
    ir = data.get("ir") or {}
    return {
        "carrier_freq": ir.get("carrier_freq", _IR_DEFAULTS["carrier_freq"]),
        "duty_33": ir.get("duty_33", _IR_DEFAULTS["duty_33"]),
    }


def get_detection(cfg=None):
    """Latch / scan parameters and channel count."""
    data = _as_dict(cfg)
    det = data.get("detection") or {}
    return {
        "num_bells": det.get("num_bells", _DET_DEFAULTS["num_bells"]),
        "mode": det.get("mode", _DET_DEFAULTS["mode"]),
        "latch_timeout_ms": det.get(
            "latch_timeout_ms", _DET_DEFAULTS["latch_timeout_ms"]
        ),
        "min_middle_ms": det.get("min_middle_ms", _DET_DEFAULTS["min_middle_ms"]),
        "max_middle_ms": det.get("max_middle_ms", _DET_DEFAULTS["max_middle_ms"]),
        "scan_interval_ms": det.get(
            "scan_interval_ms", _DET_DEFAULTS["scan_interval_ms"]
        ),
    }


def create_hardware(cfg=None):
    """
    Create bell-detector hardware objects + resolved settings dicts.

    Only claims detection.num_bells input and LED pins.
    """
    data = _as_dict(cfg)
    pins = get_pins(data)
    ir = get_ir(data)
    det = get_detection(data)
    num_bells = int(det["num_bells"])

    family = (data.get("device") or {}).get("family", "sandbells")
    if str(family).lower() not in ("sandbells", "sandswing"):
        print(
            "hardware_config: family=%r – IR bell GPIO factory; "
            "sensing profiles use a different module" % (family,)
        )

    inputs = []
    for i in range(num_bells):
        pin = digitalio.DigitalInOut(
            getattr(board, "GP%d" % (pins["input_base"] + i))
        )
        pin.direction = digitalio.Direction.INPUT
        inputs.append(pin)

    leds = []
    for i in range(num_bells):
        pin = digitalio.DigitalInOut(
            getattr(board, "GP%d" % (pins["output_base"] + i))
        )
        pin.direction = digitalio.Direction.OUTPUT
        pin.value = True  # inverted: True = OFF
        leds.append(pin)

    tx_led = digitalio.DigitalInOut(getattr(board, "GP%d" % pins["tx_led"]))
    tx_led.direction = digitalio.Direction.OUTPUT
    tx_led.value = True

    status_led = digitalio.DigitalInOut(
        getattr(board, "GP%d" % pins["status_led"])
    )
    status_led.direction = digitalio.Direction.OUTPUT
    status_led.value = True

    ir_pwm = pwmio.PWMOut(
        getattr(board, "GP%d" % pins["ir_led"]),
        frequency=ir["carrier_freq"],
        duty_cycle=ir["duty_33"],
    )

    return {
        "inputs": inputs,
        "leds": leds,
        "ir_pwm": ir_pwm,
        "tx_led": tx_led,
        "status_led": status_led,
        "pins": pins,
        "ir": ir,
        "detection": det,
    }