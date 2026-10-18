# Every src module imports with the fake board modules (CircuitPython-style
# names only; code.py and boot.py are launchers and run at import, so skip).
import os
import sys
from hw import spawn, child_setup, child_finish, check, finish, NEXT_SRC

SKIP = ("code.py", "boot.py")


def child(scenario, src, extra):
    fh = child_setup(src, extra)
    out = {"ok": True, "failed": {}}
    for name in sorted(os.listdir(src)):
        if name.endswith(".py") and name not in SKIP:
            try:
                __import__(name[:-3])
            except Exception as e:
                out["failed"][name] = "%s: %s" % (type(e).__name__, e)
    out["ok"] = not out["failed"]
    child_finish(out, fh)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        mods = [n for n in os.listdir(NEXT_SRC) if n.endswith(".py") and n not in SKIP]
        r = spawn(os.path.abspath(__file__), "imports")
        check("all %d modules import with fakes" % len(mods), r["ok"], r.get("failed") or r.get("exc"))
        check("importing claims no pin", not r.get("claims"), r.get("claims"))
        finish()
