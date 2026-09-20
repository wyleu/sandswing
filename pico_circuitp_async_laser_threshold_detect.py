# pico_circuitp_async_laser_threshold_detect.py
#
# Laser drive threshold detect → write settings.json (Sand* farm)
# ----------------------------------------------------------------
# Filename: pico_circuitp_async_laser_threshold_detect.py
#
# Overview
# --------
# For each optical head, ramp laser PWM from low to high until the
# phototransistor changes state (first reliable edge). That duty is
# treated as the minimum “beam seen” level for the current aim.
#
# Operating duty is stored with headroom:
#   drive = min(65535, int(threshold * margin))
#
# Results are written into settings.json:
#   sensing.live.drive_duties   – per-channel list for live / basic detect
#   sensing.live.drive_duty     – max of the list (legacy single default)
#   sensing.test.drive_duty     – same max (lineup / path tests)
#   sensing.test.drive_duties   – per-channel list (optional use by tests)
#
# Bare optics only: no MIDI velocity curves. Neo shows active channel.
# Console + log report each step and the final JSON update.
#
# Shared modules
# --------------
#   config_loader.py – load / merge / save settings.json
#   hardware_config.py, sand_emit.py, sand_neo.py, sand_optical.py
#
# settings.json (read / written)
# ------------------------------
#   "sensing": {
#     "live": {
#       "invert_pt": true,
#       "drive_duty": 20000,
#       "drive_duties": [12000, 12000]
#     },
#     "test": {
#       "drive_duty": 20000,
#       "drive_duties": [12000, 12000],
#       "threshold_margin": 1.3,
#       "threshold_start": 0,
#       "threshold_step": 512,
#       "threshold_settle_s": 0.08
#     }
#   }
#
# Launch
# ------
#   "startup": { "program": "pico_circuitp_async_laser_threshold_detect.py" }
# After a successful run, point startup back at basic_detect or sandswing.

import time
import json

from config_loader import load_config, CONFIG_PATH
from hardware_config import create_hardware
from sand_emit import open_log, close_log, emit
from sand_neo import SandNeo
from sand_optical import OpticalHeads

# ---------------------------------------------------------------------------
cfg = load_config()
cfg.banner()

hw = create_hardware(cfg)
pins = hw["pins"]
det = hw["detection"]
num = int(det.get("num_bells", 2))

live = {}
test = {}
try:
    if cfg.raw and isinstance(cfg.raw.get("sensing"), dict):
        live = dict(cfg.raw["sensing"].get("live") or {})
        test = dict(cfg.raw["sensing"].get("test") or {})
except Exception:
    pass

INVERT_PT = bool(live.get("invert_pt", True))
MARGIN = float(test.get("threshold_margin", 1.3))
START = int(test.get("threshold_start", 0))
STEP = int(test.get("threshold_step", 512))
SETTLE_S = float(test.get("threshold_settle_s", 0.08))
MAX_DUTY = 65535

heads = OpticalHeads.from_hw(hw, num=num, invert_pt=INVERT_PT)
neo = SandNeo.from_cfg(cfg, pins, heads.num)
open_log(cfg, suffix="threshold.txt")

status_led = hw.get("status_led")
if status_led:
    status_led.value = True


def neo_ch(ch):
    if not neo or not neo.strip:
        return
    neo.strip[0] = neo.status_color
    for i in range(1, len(neo.strip)):
        neo.strip[i] = (0, 32, 0) if (i == ch + 1) else (0, 0, 0)
    neo.strip.show()


def find_threshold(ch):
    """
    Ramp duty; return first duty where PT differs from the level at duty=0.
    Returns None if no change up to MAX_DUTY.
    """
    heads.all_off()
    time.sleep(SETTLE_S)
    baseline = heads.read_pt(ch)
    emit(
        "ch %d baseline PT=%s at duty=0  (%s)"
        % (ch + 1, baseline, heads.describe(ch))
    )

    found = None
    duty = START
    while duty <= MAX_DUTY:
        heads.laser_pwms[ch].duty_cycle = duty
        for o in range(heads.num):
            if o != ch:
                heads.laser_pwms[o].duty_cycle = 0
        time.sleep(SETTLE_S)
        raw = heads.read_pt(ch)
        emit("ch %d duty=%5d PT=%s" % (ch + 1, duty, raw))
        if raw != baseline:
            found = duty
            emit("ch %d THRESHOLD duty=%d (PT %s → %s)" % (ch + 1, duty, baseline, raw))
            break
        duty += STEP
        if duty > MAX_DUTY and found is None:
            # last try at full
            heads.laser_pwms[ch].duty_cycle = MAX_DUTY
            time.sleep(SETTLE_S)
            if heads.read_pt(ch) != baseline:
                found = MAX_DUTY
                emit("ch %d THRESHOLD duty=%d (at max)" % (ch + 1, found))
            break

    heads.all_off()
    return found


def apply_margin(threshold):
    if threshold is None:
        return None
    d = int(threshold * MARGIN)
    if d < threshold:
        d = threshold
    if d > MAX_DUTY:
        d = MAX_DUTY
    return d


def save_duties(drive_list):
    """Update cfg.raw sensing.live / sensing.test and write settings.json."""
    if cfg.raw is None:
        cfg.raw = {}
    if "sensing" not in cfg.raw or not isinstance(cfg.raw["sensing"], dict):
        cfg.raw["sensing"] = {}
    sensing = cfg.raw["sensing"]
    if "live" not in sensing or not isinstance(sensing["live"], dict):
        sensing["live"] = {}
    if "test" not in sensing or not isinstance(sensing["test"], dict):
        sensing["test"] = {}

    live_sec = sensing["live"]
    test_sec = sensing["test"]

    live_sec["drive_duties"] = list(drive_list)
    test_sec["drive_duties"] = list(drive_list)

    legacy = max(drive_list) if drive_list else int(live.get("drive_duty", 20000))
    live_sec["drive_duty"] = legacy
    test_sec["drive_duty"] = legacy
    live_sec["invert_pt"] = INVERT_PT

    path = CONFIG_PATH if "CONFIG_PATH" in dir() else "settings.json"
    try:
        from config_loader import CONFIG_PATH as _p
        path = _p
    except Exception:
        path = "settings.json"

    # Prefer Config.save if present
    ok = False
    if hasattr(cfg, "save"):
        try:
            ok = bool(cfg.save(path))
        except Exception as e:
            emit("cfg.save failed: %s" % e)

    if not ok:
        try:
            with open(path, "w") as f:
                json.dump(cfg.raw, f)
            ok = True
        except OSError as e:
            emit("write %s failed: %s" % (path, e))
            return False

    emit("Wrote %s" % path)
    emit("  sensing.live.drive_duties  = %s" % (drive_list,))
    emit("  sensing.live.drive_duty    = %s" % legacy)
    emit("  sensing.test.drive_duties  = %s" % (drive_list,))
    emit("  sensing.test.drive_duty    = %s" % legacy)
    return True


def main():
    emit(
        "THRESHOLD DETECT START channels=%d step=%d margin=%.2f"
        % (heads.num, STEP, MARGIN)
    )
    thresholds = []
    drives = []

    for ch in range(heads.num):
        neo_ch(ch)
        emit("--- channel %d ---" % (ch + 1,))
        th = find_threshold(ch)
        thresholds.append(th)
        drv = apply_margin(th) if th is not None else int(live.get("drive_duty", 20000))
        if th is None:
            emit("ch %d NO THRESHOLD – keeping fallback duty=%d" % (ch + 1, drv))
        else:
            emit("ch %d operate duty=%d (threshold %d × %.2f)" % (ch + 1, drv, th, MARGIN))
        drives.append(drv)

    heads.all_off()
    if neo:
        neo.all_off()

    emit("Result thresholds=%s drives=%s" % (thresholds, drives))
    save_duties(drives)
    emit("THRESHOLD DETECT DONE – set startup.program back to basic_detect or sandswing")


try:
    main()
    import json
    print(json.load(open("settings.json"))["sensing"])
except KeyboardInterrupt:
    print("Stopped")
finally:
    heads.all_off()
    if neo:
        neo.all_off()
    close_log()
    print("Cleanup complete")