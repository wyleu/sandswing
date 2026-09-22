"""
pins_from_settings.py
=====================

Pin map for the sandswing / optical-lineup firmware on a Pico 2 W.

WHY THIS FILE EXISTS
    The lineup code used to build laser pins as
        output_base + channel_index
    and sense pins as
        input_base + channel_index
    That is how GP numbers drifted away from the loom and the lasers
    looked dead. All GP numbers now come from settings.json → "pins"
    as explicit lists. No arithmetic.

SAVE THIS FILE AS
    /CIRCUITPY/pins_from_settings.py
    (same folder as code.py and settings.json)

THIS LOOM (2026-09-20)
    Sense (photodiode, digital, active-low, pull-up):
        channel 0 → GP0   HEAD FITTED
        channel 1 → GP1   HEAD FITTED
        channel 2 → GP2   no head
        channel 3 → GP3   no head

    Laser / IR drive (PWMOut, 1 kHz, duty 0..65535):
        channel 0 → GP7
        channel 1 → GP8
        channel 2 → GP9
        channel 3 → GP10

    NeoPixel data: GP16
    Optional laser-current ADC: GP26 (ch0), GP27 (ch1) only
    Optional shared PT analogue: GP28

    "fitted": [0, 1] means only channels 0 and 1 have optics.
    PWM objects are still created for GP9/GP10 so the lists stay aligned.

REQUIRED settings.json FRAGMENT
    {
      "pins": {
        "sense": [0, 1, 2, 3],
        "laser_pwm": [7, 8, 9, 10],
        "neopixel": 16,
        "adc_laser": [26, 27],
        "adc_pt": 28,
        "fitted": [0, 1]
      },
      "detection": { "num_bells": 4 }
    }

    Alias: "drive" is accepted if "laser_pwm" is missing.

HOW load_config() IS PROBED
    1. cfg.get("pins")           # dict-like Config
    2. cfg.pins                  # attribute
    3. cfg.data["pins"] or cfg._data["pins"]
    If none of those exist, the defaults above (0-3 / 7-10 / 16) are used
    so a missing key does not silently go back to output_base arithmetic.

WHAT claim_lineup_hardware() DOES
    - deinit() any LED objects passed in (from create_hardware)
    - DigitalInOut INPUT + PULL_UP on every sense GP
    - PWMOut 1 kHz duty 0 on every laser GP
    - AnalogIn on adc_laser / adc_pt; skip and log if the pin is busy
    - prints: PINS sense […] laser […] neo N fitted […]

WHAT THIS FILE DOES NOT DO
    - Does not read input_base / output_base
    - Does not turn lasers on (duty stays 0 until sandswing sweeps)
    - Does not create the NeoPixel object (code.py does that from neopixel_gp)
    - Does not know about Wi-Fi, MIDI, or WebSocket
    - Does not rewrite the CIRCUITPY volume label

USAGE FROM code.py
    from pins_from_settings import load_pinmap, claim_lineup_hardware

    cfg = load_config()
    hw = create_hardware(cfg)          # optional; we steal/deinit its leds
    pinmap = load_pinmap(cfg)
    NUM = min(len(pinmap["sense_gp"]),
              len(pinmap["laser_gp"]),
              int(hw["detection"].get("num_bells", 4)))
    inputs, laser_pwms, adc_laser, adc_pt = claim_lineup_hardware(
        pinmap, existing_leds=hw.get("leds")
    )
    LASER_GPS = pinmap["laser_gp"]
    SENSE_GPS = pinmap["sense_gp"]

BOOT CHECK
    You must see:
        PINS sense [0, 1, 2, 3] laser [7, 8, 9, 10] neo 16 fitted [0, 1]
        --- ch 1 laser GP7 PT GP0 ---
    If the log still says output_base or laser GP2, code.py is not using
    this module.
"""

import board
import digitalio
import pwmio
import analogio


def _gp(n):
    return getattr(board, "GP%d" % int(n))


def load_pinmap(cfg):
    """
    Read settings.json "pins" from a load_config() object.

    Returns dict:
        sense_gp, laser_gp, fitted, neopixel_gp, adc_laser_gp, adc_pt_gp
    """
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

    sense = list(raw.get("sense") or [0, 1, 2, 3])
    laser = list(raw.get("laser_pwm") or raw.get("drive") or [7, 8, 9, 10])
    if len(sense) != len(laser):
        raise ValueError("pins.sense and pins.laser_pwm must be the same length")

    fitted = list(raw.get("fitted") or range(len(sense)))
    neo = int(raw.get("neopixel", 16))
    adc_laser = list(raw.get("adc_laser") or [26, 27])
    adc_pt = raw.get("adc_pt", 28)

    mux = raw.get("adc_mux") or {}
    return {
        "sense_gp": [int(x) for x in sense],
        "laser_gp": [int(x) for x in laser],
        "sense": [int(x) for x in sense],
        "laser_pwm": [int(x) for x in laser],
        "fitted": [int(x) for x in fitted],
        "neopixel_gp": neo,
        "adc_laser_gp": [int(x) for x in adc_laser],
        "adc_pt_gp": None if adc_pt is None else int(adc_pt),
        "adc_mux": mux,
}


def claim_lineup_hardware(pinmap, existing_leds=None):
    """
    Claim sense inputs and laser PWMs from pinmap.
    existing_leds: optional list of DigitalInOut/PWMOut to deinit first
    so GP7-10 are free.
    """
    if existing_leds:
        for led in existing_leds:
            try:
                led.deinit()
            except Exception:
                pass

    inputs = []
    for gp in pinmap["sense_gp"]:
        pin = digitalio.DigitalInOut(_gp(gp))
        pin.direction = digitalio.Direction.INPUT
        pin.pull = digitalio.Pull.UP
        inputs.append(pin)

    lasers = []
    for gp in pinmap["laser_gp"]:
        lasers.append(pwmio.PWMOut(_gp(gp), frequency=1000, duty_cycle=0))

    adc_laser = []
    for gp in pinmap["adc_laser_gp"]:
        try:
            adc_laser.append(analogio.AnalogIn(_gp(gp)))
        except Exception as e:
            print("ADC laser GP%d skip:" % gp, e)

    adc_pt = None
    if pinmap["adc_pt_gp"] is not None:
        try:
            adc_pt = analogio.AnalogIn(_gp(pinmap["adc_pt_gp"]))
        except Exception as e:
            print("ADC PT skip:", e)

    print("PINS sense", pinmap["sense_gp"], "laser", pinmap["laser_gp"],
          "neo", pinmap["neopixel_gp"], "fitted", pinmap["fitted"])
    return inputs, lasers, adc_laser, adc_pt
