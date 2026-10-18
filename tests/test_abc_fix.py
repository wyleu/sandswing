# test_abc_fix.py -- A/B/C ownership harness, pointed at sandswing_next.
#
#   python3 tests/test_abc_fix.py
#
# Same 23 checks as /workspace/abc_fix/test_abc_fix.py.  ORIGINAL is still
# the repo clone (must fail with 'GP18 in use').  PATCHED is now
# sandswing_next/src on its own (no overlay).  Checks that named retired
# functions are mapped to their replacements:
#   claim_muxes/claim_threshold idempotent -> build_from_pins idempotent
#   one MuxAddress shared                  -> one Tower shared by both paths
#   late Mux4051 shares                    -> late get_address/get_analog share
#   scan_heads without addr while held     -> rescan while held (no claim)
#   standalone scan_heads claims+releases  -> close() then a new Concentrator
#
# Fake board/digitalio/analogio/pwmio/neopixel/wifi raise ValueError('GPxx in
# use') on a double claim, like CircuitPython.  Each scenario runs in a fresh
# subprocess with src = ORIGINAL or PATCHED.
#
# ORIGINAL = $SANDSWING_ORIG_SRC, else this repo's own src/.  The 3 ORIGINAL
# checks need the pre-restructure files (head_probe.py, mux4051.py); when
# they are not there (they are retired on branch 'restructure') the checks
# are SKIPPED, not failed:  "skipped: original code retired".
#   SANDSWING_ORIG_SRC=/path/to/old/src python3 tests/test_abc_fix.py
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PATCHED = os.path.join(os.path.dirname(HERE), "src")
ORIG = os.environ.get("SANDSWING_ORIG_SRC") or PATCHED
ORIG_FILES = ("head_probe.py", "mux4051.py")


def have_original():
    return all(os.path.exists(os.path.join(ORIG, f)) for f in ORIG_FILES)
SETTINGS = os.path.join(HERE, "data", "settings_current.json")


# ---------------------------------------------------------------- child side
def child(variant, settings_variant, scenario):
    import time
    time.sleep = lambda s: None                  # probe timings not needed
    sys.path[:0] = [os.path.join(HERE, "fakes"), os.path.join(HERE, "fakes", "stubs")]
    sys.path.insert(2, PATCHED if variant == "patched" else ORIG)
    import _fake_hw
    out = {"ok": False}
    try:
        if scenario == "concentrator":
            import concentrator
            app = concentrator.Concentrator(concentrator.cfg)
            app._probe_heads()
            app.rescan()
            app.rescan()
            out["heads"] = list(app.head)
            out["thr_pwm_hz"] = getattr(app.thr_pwm, "frequency", None)
            import mux_select
            from tower_build import build_from_pins
            out["idempotent"] = build_from_pins(app.pinmap["raw"]) == (app.mux, app.bells)
            out["shared_addr"] = all(b.sense.tower is app.mux and b.drive.tower is app.mux
                                     for b in app.bells)
            out["thr_common_adc"] = 27 not in _fake_hw.claimed
            out["late_mux_shares"] = (mux_select.get_address(18, 19, 20) is app.mux and
                                      mux_select.get_analog(26) is app.bells[0].sense.adc)
            app.rescan()                         # while held: borrows, no claim
        elif scenario == "standalone":
            import concentrator
            app = concentrator.Concentrator(concentrator.cfg)
            app.close()
            out["released"] = sorted(_fake_hw.claimed) == []
            app = concentrator.Concentrator(concentrator.cfg)
            out["heads"] = list(app.head)
        out["ok"] = True
    except Exception as e:
        out["exc"] = "%s: %s" % (type(e).__name__, e)
    counts = {}
    for gp, kind in _fake_hw.history:
        counts[gp] = counts.get(gp, 0) + 1
    out["claims"] = counts
    out["errors"] = ["GP%d %s (held as %s)" % e for e in _fake_hw.errors]
    out["gp27"] = 27 in counts
    print("@@" + json.dumps(out))


# ---------------------------------------------------------------- parent side
def run(variant, settings_variant, scenario):
    d = tempfile.mkdtemp(prefix="abc_")
    s = json.load(open(SETTINGS))
    if settings_variant == "no_common":
        s["pins"]["threshold_mux"].pop("common", None)
    json.dump(s, open(os.path.join(d, "settings.json"), "w"))
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child",
                        variant, settings_variant, scenario],
                       cwd=d, capture_output=True, text=True, timeout=120)
    shutil.rmtree(d, ignore_errors=True)
    line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
    if not line:
        return {"ok": False, "exc": "no result: " + p.stderr[-800:]}
    return json.loads(line[-1][2:])


results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name,
                          "" if cond else "  -- " + str(detail)))


def skip(name, why="original code retired"):
    results.append(None)
    print("  skip  %s  -- skipped: %s" % (name, why))


def main():
    print("ORIGINAL mux_blend (harness must reproduce the bug)")
    for sv in ("with_common27", "no_common"):
        if not have_original():
            skip("original/%s fails with 'GP18 in use'" % sv)
            if sv == "with_common27":
                skip("original/%s claims phantom GP27 first" % sv)
            continue
        r = run("orig", sv, "concentrator")
        check("original/%s fails with 'GP18 in use'" % sv,
              not r["ok"] and "GP18 in use" in r.get("exc", ""), r.get("exc"))
        if sv == "with_common27":
            check("original/%s claims phantom GP27 first" % sv, r["gp27"], r["claims"])

    print("PATCHED")
    for sv in ("with_common27", "no_common"):
        r = run("patched", sv, "concentrator")
        tag = "patched/%s" % sv
        check(tag + " Concentrator + _probe_heads + rescan x2 runs", r["ok"], r.get("exc"))
        check(tag + " no 'GPxx in use' anywhere (incl. swallowed)", not r["errors"], r["errors"])
        multi = dict((g, n) for g, n in r["claims"].items() if g in ("18", "19", "20", "26", "21", "28"))
        check(tag + " A/B/C, GP26, GP21, GP28 each claimed exactly once",
              multi == {"18": 1, "19": 1, "20": 1, "26": 1, "21": 1, "28": 1}, r["claims"])
        check(tag + " GP27 never claimed", not r["gp27"], r["claims"])
        check(tag + " threshold PWM at 20 kHz", r.get("thr_pwm_hz") == 20000, r.get("thr_pwm_hz"))
        check(tag + " build_from_pins idempotent (was claim_muxes/claim_threshold)", r.get("idempotent"))
        check(tag + " one Tower shared by sense+drive, no thr common pin",
              r.get("shared_addr") and r.get("thr_common_adc"))
        check(tag + " late get_address/get_analog share Tower + GP26 (was Mux4051)",
              r.get("late_mux_shares"))
        check(tag + " heads all 'empty' with nothing plugged",
              r.get("heads") == ["empty"] * 8, r.get("heads"))

    r = run("patched", "with_common27", "standalone")
    check("patched close() releases all; second Concentrator claims clean",
          r["ok"] and not r["errors"] and r.get("released"), r)

    print("Split draft tests (tests/test_<module>.py using support.py)")
    tot_p = tot_f = 0
    for name in sorted(os.listdir(HERE)):
        if name.startswith("test_") and open(os.path.join(HERE, name)).read().startswith("# tests for "):
            p = subprocess.run([sys.executable, os.path.join(HERE, name)],
                               capture_output=True, text=True, cwd=HERE)
            m = re.findall(r"^(\d+) passed, (\d+) failed", p.stdout, re.M)
            ps, fs = (int(m[-1][0]), int(m[-1][1])) if m else (0, 1)
            tot_p, tot_f = tot_p + ps, tot_f + fs + (1 if p.returncode and not fs else 0)
    last = "%d passed, %d failed" % (tot_p, tot_f)
    check("draft tests: " + last, tot_f == 0 and tot_p == 49, last)

    n_fail = results.count(False)
    n_skip = results.count(None)
    print("\n%d passed, %d failed, %d skipped"
          % (len(results) - n_fail - n_skip, n_fail, n_skip))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
