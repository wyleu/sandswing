# farm_log.py
#
# Size-capped session log for Sand* CircuitPython boards
# ------------------------------------------------------
# Filename: farm_log.py
#
# Overview
# --------
# Tiny CIRCUITPY filesystem helper shared by sandswing / sandsense / etc.
# Appends one text log, then rotates when it reaches a byte cap so the
# volume cannot fill and truncate code.py / sandswing.py on the next save.
#
# If free flash is below a floor, logging is skipped rather than risking
# a full disc. At most two files exist: <prefix>lineup.txt (current) and
# <prefix>lineup.old (previous generation).
#
# Functional areas
# ----------------
# 1. start(cfg)  – read output.log from settings.json, open or skip
# 2. write(msg)  – append one line, rotate at max_bytes
# 3. close()     – flush and release the file (call from finally)
#
# settings.json (excerpt)
# -----------------------
#   "output": {
#     "log": {
#       "enabled": true,
#       "filename_prefix": "sandswing_",
#       "max_bytes": 80000,
#       "keep_old": true
#     }
#   }
#
#   enabled          – master switch (also honours cfg.log_enabled)
#   filename_prefix  – usually from cfg.log_prefix
#   max_bytes        – rotate threshold (default 80_000)
#   keep_old         – keep one previous file as *.old
#
# Usage
# -----
#   import farm_log
#   farm_log.start(cfg)
#   farm_log.write("a line")
#   farm_log.close()
#
# No Wi-Fi, no hardware. Safe to import on any Pico with CIRCUITPY.
#
import os

_fp = None
_path = None
_old = None
_max = 80000
_keep = True
_min_free = 32768

def _free():
    s = os.statvfs("/")
    return s[0] * s[3]

def _size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return 0

def _close():
    global _fp
    if _fp:
        try:
            _fp.close()
        except OSError:
            pass
        _fp = None

def _rotate():
    _close()
    if not _path:
        return
    try:
        if _keep:
            try:
                os.remove(_old)
            except OSError:
                pass
            os.rename(_path, _old)
        else:
            os.remove(_path)
    except OSError:
        try:
            os.remove(_path)
        except OSError:
            pass

def start(cfg):
    """Open log from settings output.log. No-op if disabled or flash low."""
    global _fp, _path, _old, _max, _keep
    if not getattr(cfg, "log_enabled", False):
        return False
    prefix = getattr(cfg, "log_prefix", "farm_")
    _path = prefix + "lineup.txt"
    _old = prefix + "lineup.old"
    _max = cfg.get_int("output.log.max_bytes", 80000)
    _keep = cfg.get_bool("output.log.keep_old", True)
    if _free() < _min_free:
        print("log skipped (flash low)")
        return False
    if _size(_path) >= _max:
        _rotate()
    try:
        _fp = open(_path, "a")
        _fp.write("log start\n")
        _fp.flush()
        print("log:", _path, "bytes", _size(_path), "free", _free())
        return True
    except OSError as e:
        print("log open failed:", e)
        _fp = None
        return False

def write(msg):
    global _fp
    if not _fp:
        return
    try:
        _fp.write(msg + "\n")
        if _size(_path) >= _max:
            _rotate()
            if _free() > _min_free:
                _fp = open(_path, "a")
            else:
                _fp = None
        else:
            _fp.flush()
    except OSError:
        _fp = None
        
def close():
    _close()