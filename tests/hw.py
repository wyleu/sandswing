# hw.py -- run a scenario in a fresh process with the fake CircuitPython
# modules (fakes/: board, digitalio, analogio, pwmio, neopixel, wifi raise
# ValueError('GPxx in use') on a double claim) in front of a src tree.
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NEXT_SRC = os.path.join(os.path.dirname(HERE), "src")
SETTINGS = os.path.join(HERE, "data", "settings_current.json")


def spawn(test_file, scenario, settings_edit=None, src=NEXT_SRC, extra_src=None,
          patch=None):
    """patch: dict merged into the top level of settings.json."""
    d = tempfile.mkdtemp(prefix="ssn_")
    s = json.load(open(SETTINGS))
    if settings_edit == "no_common":
        s["pins"]["threshold_mux"].pop("common", None)
    s.update(patch or {})
    json.dump(s, open(os.path.join(d, "settings.json"), "w"))
    args = [sys.executable, test_file, "--child", scenario, src, extra_src or ""]
    p = subprocess.run(args, cwd=d, capture_output=True, text=True, timeout=300)
    shutil.rmtree(d, ignore_errors=True)
    line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
    if not line:
        return {"ok": False, "exc": "no result: " + p.stderr[-1500:]}
    return json.loads(line[-1][2:])


def child_setup(src, extra_src=""):
    import time
    time.sleep = lambda s: None
    sys.path[:0] = [os.path.join(HERE, "fakes"), os.path.join(HERE, "fakes", "stubs"), src]
    if extra_src:
        sys.path.insert(3, extra_src)
    import _fake_hw
    return _fake_hw


# SurveyState needs ~18 steps for one idle -> examining -> surveying ->
# settled -> idle cycle; this bound only stops a stuck survey hanging a test.
SURVEY_STEP_LIMIT = 200


def run_survey(app, reason="test", channel=None):
    """Trigger app.survey_state, then await the steady loop's _sense_step()
    until the survey has settled and gone back to idle, then let panel_task
    paint once (that is what copies survey kinds into app.kinds/app.head).
    Returns the phases seen.  Replaces the old app.rescan()/_probe_heads()."""
    import asyncio
    import survey_state

    async def cycle():
        app.survey_state.trigger(reason, channel=channel)
        seen = []
        for _ in range(SURVEY_STEP_LIMIT):
            await app._sense_step()
            seen.append(app.survey_state.phase)
            if survey_state.SETTLED in seen and seen[-1] == survey_state.IDLE:
                break
        else:
            raise AssertionError("survey did not settle: %s" % seen[-5:])
        await _panel_pass(app)
        return seen

    return asyncio.run(cycle())


async def _panel_pass(app):
    import asyncio
    panel = asyncio.ensure_future(app.panel_task())
    await asyncio.sleep(0)                   # one panel_task pass
    panel.cancel()


def paint_once(app):
    """One panel_task pass (it alone refreshes app.frame now)."""
    import asyncio
    asyncio.run(_panel_pass(app))


def child_finish(out, fh):
    counts = {}
    for gp, kind in fh.history:
        counts[gp] = counts.get(gp, 0) + 1
    out["claims"] = counts
    out["errors"] = ["GP%d %s (held as %s)" % e for e in fh.errors]
    out["gp27"] = 27 in counts
    print("@@" + json.dumps(out))


results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name,
                          "" if cond else "  -- " + str(detail)[:600]))


def skip(name, why):
    results.append(None)
    print("  skip  %s  -- skipped: %s" % (name, why))


def finish():
    n_fail = results.count(False)
    n_skip = results.count(None)
    print("\n%d passed, %d failed, %d skipped"
          % (len(results) - n_fail - n_skip, n_fail, n_skip))
    sys.exit(1 if n_fail else 0)
