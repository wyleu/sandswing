# tower_build.py -- settings "pins" -> (Tower, [Bell]).  Called once by
# concentrator.py (a second call returns the same objects).
#
# Enforces, before claiming anything: no two PWM pins on one RP2350 slice
# and channel, one frequency per slice.  Then: both muxes resolve to the
# SAME Tower, each bell has the same Y on both sides, threshold_mux.common
# (GP27 in old settings) is ignored, never claimed.
from mux_errors import MuxConfigError
from mux_select import get_tower, get_analog
from sense_path import SensePath
from drive_path import DrivePath
from bell_channel import Bell
from pwm_slices import check_pwm_slices, pwm_pins_from_cfg
from connector_boards import DEFAULT_BOARDS

DEFAULT_PINS = {
    "sense": [0, 1, 2, 3, 4, 5, 6, 7],            # Blue/White, digital in
    "laser_pwm": [8, 9, 10, 11, 12, 13, 14, 15],  # Green, PWM out
    "neopixel": 16,
    "select_groups": {"tower": {"a": 18, "b": 19, "c": 20, "inh": None,
                                "settle_us": 1000}},
    "adc_mux": {"select": "tower", "common": 26, "y": [0, 1, 2, 3, 4, 5, 6, 7]},
    "threshold_mux": {"select": "tower", "pwm_gp": 21, "pwm_hz": 20000,
                      "sense_gp": 28, "y": [0, 1, 2, 3, 4, 5, 6, 7],
                      "rc": {"r1": 10e3, "c1": 100e-9, "r2": 10e3, "c2": 1e-6},
                      "hold_c": 100e-9},
    "connector_boards": DEFAULT_BOARDS,
}


def _group_for(mux_cfg, groups):
    """New style "select": "tower", or legacy inline a/b/c (implicit group
    keyed by the pin tuple, so identical a/b/c share)."""
    name = mux_cfg.get("select")
    if name is not None:
        if name not in groups:
            raise MuxConfigError("unknown select group %r" % (name,))
        return name, groups[name]
    spec = {k: mux_cfg.get(k) for k in ("a", "b", "c", "inh")}
    if spec["a"] is None:
        raise MuxConfigError("mux has neither 'select' nor a/b/c")
    return "abc_%s_%s_%s" % (spec["a"], spec["b"], spec["c"]), spec


def build_from_pins(pins_cfg=None, pins=None, frequency=20000,
                    freq_mismatch="raise"):
    """pins_cfg = settings["pins"] (or the whole settings dict)."""
    if pins_cfg is None:
        pins_cfg = DEFAULT_PINS
    if "pins" in pins_cfg:
        pins_cfg = pins_cfg["pins"]
    groups = pins_cfg.get("select_groups") or {}
    sense_cfg = pins_cfg.get("adc_mux") or {}
    drive_cfg = pins_cfg.get("threshold_mux") or {}
    pwm_table, pwm_warn = check_pwm_slices(pwm_pins_from_cfg(pins_cfg, frequency),
                                           freq_mismatch)
    sname, sspec = _group_for(sense_cfg, groups)
    tower = get_tower(sname, sspec, pins)
    if getattr(tower, "built", None) is not None:
        return tower.built                       # idempotent
    if drive_cfg:
        dname, dspec = _group_for(drive_cfg, groups)
        if get_tower(dname, dspec, pins) is not tower:
            raise MuxConfigError("sense and drive muxes must share one select "
                                 "group (%r vs %r)" % (sname, dname))
    n = len(pins_cfg.get("sense") or [])
    sy = list(sense_cfg.get("y") or sense_cfg.get("laser_y") or range(8))
    dy = list(drive_cfg.get("y") or sy) if drive_cfg else sy
    for i in range(n):
        if sy[i] != dy[i]:
            raise MuxConfigError("bell %d: sense y=%d != drive y=%d (shared-"
                                 "channel invariant)" % (i, sy[i], dy[i]))
    tower.warnings = list(pwm_warn)
    tower.pwm_slices = pwm_table
    if drive_cfg.get("common") is not None:
        tower.warnings.append("threshold_mux.common=%r ignored (no pin on the "
                              "output COMMON except GP28 monitor)"
                              % (drive_cfg.get("common"),))
    sense = SensePath(tower, get_analog(sense_cfg.get("common", 26), tower.pins))
    drive = None
    if drive_cfg and drive_cfg.get("pwm_gp") is not None:
        rc = drive_cfg.get("rc")
        rc = None if not rc else (rc["r1"], rc["c1"], rc["r2"], rc["c2"])
        sgp = drive_cfg.get("sense_gp")
        pwm = tower.pins.pwm_out(drive_cfg["pwm_gp"],
                                 frequency=int(drive_cfg.get("pwm_hz", frequency)),
                                 duty_cycle=0)
        drive = DrivePath(tower, pwm,
                          None if sgp is None else get_analog(sgp, tower.pins),
                          rc=rc, tol_counts=int(drive_cfg.get("readback_tol", 1024)))
    bells = [Bell(i, sy[i], tower, sense, drive) for i in range(n)]
    tower.built = (tower, bells)
    return tower.built
