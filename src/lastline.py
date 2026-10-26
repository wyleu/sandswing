# lastline.py
#
# The Pico's most recent log line, kept so page 3 can show it
# -----------------------------------------------------------
# A module that does `from lastline import print` gets a print() that still
# prints exactly as before and also remembers the line.  The status page puts
# get() in its top-bar summary, so what mpremote would show last is visible
# on the Sandbells display without a laptop.
#
# Routine noise (the 60 s "mem free" lines) is printed but not remembered,
# or it would push out the line that matters.
#
import builtins

_last = [""]
QUIET = ("mem free",)
MAX_LEN = 100

_print = builtins.print


def print(*args, **kw):
    _print(*args, **kw)
    try:
        s = " ".join(str(a) for a in args)
    except Exception:
        return
    if s and not s.startswith(QUIET):
        _last[0] = s.strip()[:MAX_LEN]


def get():
    return _last[0]
