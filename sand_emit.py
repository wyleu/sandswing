# sand_emit.py
#
# Shared console + log emit for Sand* farm programs
# -------------------------------------------------
# Filename: sand_emit.py
#
# Overview
# --------
# Single place for text output used by lineup tests and live apps.
# Console is always on; file log follows settings (log enabled + prefix).
#
# Typical use
# -----------
#   from sand_emit import open_log, close_log, emit
#   open_log(cfg)
#   emit("hello")
#   close_log()
#
# Does not parse settings.json itself — pass Config from config_loader.

import time

_log_file = None

def open_log(cfg, suffix="session.txt"):
    """
    Open append log if cfg.log_enabled.
    Filename: {log_prefix}{suffix}
    """
    global _log_file
    close_log()
    if not getattr(cfg, "log_enabled", False):
        return None
    prefix = getattr(cfg, "log_prefix", "sand_") or "sand_"
    name = prefix + suffix
    try:
        _log_file = open(name, "a")
        _log_file.write("Log start monotonic=%.3f\n" % time.monotonic())
        _log_file.flush()
        print("log:", name)
        return _log_file
    except OSError as e:
        print("log open failed:", e)
        _log_file = None
        return None

def close_log():
    global _log_file
    if _log_file:
        try:
            _log_file.close()
        except Exception:
            pass
        _log_file = None
        
def _ts():
    try:
        t = time.localtime()
        return "%04d-%02d-%02d %02d:%02d:%02d" % (
            t.tm_year, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec,
        )
    except Exception:
        return "%.3f" % time.monotonic()

def emit(msg):
    line = "%s %s" % (_ts(), msg)
    print(line)
    if _log_file:
        try:
            _log_file.write(line + "\n")
            _log_file.flush()
        except OSError:
            pass