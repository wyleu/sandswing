# config_loader.py
#
# Unified settings.json loader for the Sand* farm
# ------------------------------------------------
# Filename: config_loader.py
#
# Overview
# --------
# Single configuration entry point for all devices in the farm:
#
#   • sandbells     – IR bell latch / MIDI / NeoPixel programs
#   • sandsense     – tower 3-axis optical escapement / chime / strike head
#   • sandswing     – 8×1 laser heads (WIP; same schema, thinner profile)
#   • hosts         – sandbells.local / sandbells2.local (same JSON shape)
#
# Design rules (Sandsense refactor)
# ---------------------------------
# • One file format across the farm: settings.json (not settings.toml).
# • Missing sections are filled from DEFAULTS so WIP boards still boot.
# • device.name = instance id; device.family = which hardware profile to use.
# • startup.program = launcher target (exec or import).
# • Application code must not call json.load("settings.json") itself —
#   always go through load_config() / get_cfg().
# • hardware_config.py (bell GPIO factory) and future sensing profiles
#   consume this module; they do not re-implement JSON parsing.
#
# Expected top-level sections (any may be omitted)
# ------------------------------------------------
#   wifi         – ssid / password (optional if set elsewhere)
#   device       – name, family, location, role
#   startup      – program filename or program registry key
#   pins         – GPIO map (bell IR path)
#   ir           – carrier / duty (bell IR path)
#   detection    – num_bells, latch timings (bell path)
#   hardware     – head counts, laser_via, notes (sense path)
#   escapement   – tick rate, hysteresis, PLL flag, report intervals
#   checkpoint   – reboot gap bookmark
#   output       – enabled_formats, console / log / midi / neopixel / …
#
# Typical usage
# -------------
#   from config_loader import load_config, get_cfg
#
#   cfg = load_config()           # reads settings.json once, sets module cfg
#   cfg.banner()
#   print(cfg.device_name, cfg.family)
#   hyst = cfg.get_int("escapement.hysteresis", 2500)
#   if cfg.is_sandsense:
#       ...
#
#   # In-program update and save:
#   cfg.raw["escapement"]["hysteresis"] = 3000
#   cfg.save()
#
# Relationship to other modules
# -----------------------------
#   config_loader.py     – THIS FILE (JSON only)
#   hardware_config.py   – create_hardware() for IR bells (uses load_config)
#   hardware_profile.py  – (future) Sandsense / Sandswing ADS + laser drive
#   sensing / logutil    – (shared) read thresholds and flags from cfg
#
# CircuitPython + CPython compatible.

import json

try:
    import os
except ImportError:
    os = None

CONFIG_PATH = "settings.json"

# ---------------------------------------------------------------------------
# defaults – thin enough for WIP boards; real values come from JSON
# ---------------------------------------------------------------------------
DEFAULTS = {
    "wifi": {
        "ssid": "",
        "password": "",
    },
    "device": {
        "name": "unknown",
        "family": "sandbells",
        "location": "",
        "role": "",
    },
    "startup": {
        "program": None,
    },
    "pins": {},
    "ir": {},
    "detection": {},
    "hardware": {},
    "escapement": {
        "pll_enabled": False,
        "ticks_per_minute": 48,
        "adc_alpha": 0.002,
        "hysteresis": 2500,
        "adc_read_interval": 0.015,
        "report_interval": 60.0,
        "summary_interval": 900.0,
        "tick_adc_channel": 0,
        "laser_on_level": 80,
        "laser_flash_interval": 0.625,
        "laser_mode": "continuous",
        "tick_note": 76,
        "tick_velocity": 70,
        "velocity_min": 50,
        "velocity_max": 90,
        "tick_led_enabled": True,
        "tick_led_red": "0x400000",
        "tick_led_green": "0x004000",
    },
    "checkpoint": {
        "enabled": True,
        "file": "escapement_ckpt.txt",
        "max_gap_s": 172800,
    },
    "output": {
        "enabled_formats": ["console", "log"],
        "console": {"enabled": True},
        "log": {
            "enabled": True,
            "filename_prefix": "session_",
            "timestamp_format": "%Y%m%d_%H%M%S",
        },
        "midi": {
            "enabled": False,
            "channel": 0,
            "note_base": 60,
            "velocity": 100,
            "note_duration_ms": 500,
        },
        "neopixel": {"enabled": False},
        "test": {"enabled": False},
        "abell": {"enabled": False},
    },
}


def _deep_merge(base, overlay):
    """Recursively merge overlay onto a copy of base (dicts only)."""
    out = {}
    for k, v in base.items():
        out[k] = v
    if not overlay:
        return out
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except OSError as e:
        print("config_loader: could not open", path, "→", e)
        return {}
    except ValueError as e:
        # CircuitPython json uses ValueError for decode errors
        print("config_loader: invalid JSON in", path, "→", e)
        return {}


def load_raw(path=CONFIG_PATH):
    """Return merged dict (defaults + file). Does not set module global."""
    file_cfg = _read_json(path)
    return _deep_merge(DEFAULTS, file_cfg)


class Config:
    """
    Convenient view over settings.json.
    Prefer attribute access for common fields; use section dicts for the rest.
    """

    def __init__(self, data):
        self.raw = data
        self.wifi = data.get("wifi", {})
        self.device = data.get("device", {})
        self.startup = data.get("startup", {})
        self.pins = data.get("pins", {})
        self.ir = data.get("ir", {})
        self.detection = data.get("detection", {})
        self.hardware = data.get("hardware", {})
        self.escapement = data.get("escapement", {})
        self.checkpoint = data.get("checkpoint", {})
        self.output = data.get("output", {})

    # --- device identity ---
    @property
    def device_name(self):
        return self.device.get("name", "unknown")

    @property
    def family(self):
        return str(self.device.get("family", "sandbells")).lower()

    @property
    def location(self):
        return self.device.get("location", "")

    @property
    def role(self):
        return self.device.get("role", "")

    @property
    def startup_program(self):
        return self.startup.get("program")

    # --- family helpers ---
    @property
    def is_sandsense(self):
        return self.family == "sandsense"

    @property
    def is_sandswing(self):
        return self.family == "sandswing"

    @property
    def is_sandbells(self):
        return self.family == "sandbells"

    # --- output flags ---
    def format_enabled(self, name):
        formats = self.output.get("enabled_formats") or []
        if name in formats:
            return True
        section = self.output.get(name)
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
        return False

    @property
    def console_enabled(self):
        return self.format_enabled("console")

    @property
    def log_enabled(self):
        return self.format_enabled("log")

    @property
    def midi_enabled(self):
        return self.format_enabled("midi")

    @property
    def log_prefix(self):
        log = self.output.get("log") or {}
        return log.get("filename_prefix", "session_")

    # --- typed getters (path = "escapement.hysteresis") ---
    def get(self, path, default=None):
        cur = self.raw
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_int(self, path, default=0):
        v = self.get(path, default)
        try:
            if isinstance(v, str) and v.startswith("0x"):
                return int(v, 0)
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_float(self, path, default=0.0):
        v = self.get(path, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, path, default=False):
        v = self.get(path, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    def get_str(self, path, default=""):
        v = self.get(path, default)
        return default if v is None else str(v)

    def get_hex(self, path, default=0):
        v = self.get(path, default)
        try:
            if isinstance(v, int):
                return v
            return int(str(v).strip(), 0)
        except (TypeError, ValueError):
            return default

    def save(self, path=CONFIG_PATH):
        """Write current raw dict back to disk (in-program parameter updates)."""
        try:
            with open(path, "w") as f:
                json.dump(self.raw, f)
            return True
        except OSError as e:
            print("config_loader: save failed", e)
            return False

    def banner(self):
        print("=== settings.json ===")
        print(" device :", self.device_name)
        print(" family :", self.family)
        print(" role   :", self.role or "-")
        print(" location:", self.location or "-")
        print(" program:", self.startup_program or "(none)")
        print(" console:", self.console_enabled, "log:", self.log_enabled, "midi:", self.midi_enabled)
        if self.escapement:
            print(
                " escapement: tpm=",
                self.escapement.get("ticks_per_minute"),
                "pll=",
                self.escapement.get("pll_enabled"),
                "hyst=",
                self.escapement.get("hysteresis"),
            )
        print("=====================")


# Module-level singleton filled by load_config()
cfg = None


def load_config(path=CONFIG_PATH):
    """
    Load and merge settings.json, set module-level cfg, return Config.
    Safe to call more than once (reloads).
    """
    global cfg
    data = load_raw(path)
    cfg = Config(data)
    return cfg


def get_cfg():
    """Return cfg, loading defaults+file if needed."""
    global cfg
    if cfg is None:
        return load_config()
    return cfg

# ---------------------------------------------------------------------------
# Standalone dump
# CircuitPython + Thonny often does not set __name__ == "__main__", so call:
#
#   from config_loader import dump_settings
#   dump_settings()
#
# Desktop: python config_loader.py
# ---------------------------------------------------------------------------
def dump_settings():
    """Print DEFAULTS and merged settings.json (for bring-up / debug)."""
    print("config_loader dump_settings()")
    print("CONFIG_PATH =", CONFIG_PATH)
    print()
    print("--- DEFAULTS (before merge with file) ---")
    try:
        print(json.dumps(DEFAULTS, indent=2))
    except TypeError:
        print(DEFAULTS)
    print()
    print("--- load_config() (defaults + settings.json if present) ---")
    c = load_config()
    c.banner()
    try:
        print(json.dumps(c.raw, indent=2))
    except TypeError:
        print(c.raw)


if __name__ == "__main__":
    dump_settings()