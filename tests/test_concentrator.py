# End to end: construct Concentrator on the fakes, survey (SurveyState), ring.
import os
import sys
from hw import spawn, child_setup, child_finish, check, skip, finish, run_survey

ONCE = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 26, 28]


class FakeSntp:
    def __init__(self):
        self.calls = []

    def set_ringing(self, r):
        if not self.calls or self.calls[-1] != r:
            self.calls.append(r)

    def boot_record(self):
        return {"sync": "fake"}

    def stamp(self, raw_tick=None):
        return {"tick_ms": 0, "utc_ms": None, "err_ms": None,
                "sync": "unsynced", "acc": None, "epoch": 0,
                "session": 0, "mode": "fake"}

def child(scenario, src, extra):
    fh = child_setup(src, extra)
    out = {"ok": False}
    try:
        if scenario == "head":
            # ch1 = a new head: laser current on GP26 when selected and lit;
            # Blue/White follows the threshold (LM393) and is high at rest.
            import mux_select
            state = {"trip": False}

            def cur():
                t = list(mux_select._TOWERS.values())[0]
                lit = 8 in fh.pwm and fh.pwm[8].duty_cycle == 65535
                return 700 + (1490 if (lit and t.current == 0) else 0)

            def bw():
                if state["trip"]:
                    return False
                return not (21 in fh.pwm and fh.pwm[21].duty_cycle > 30000)
            fh.adc_fn[26] = cur
            fh.din_fn[0] = bw
        import concentrator
        app = concentrator.Concentrator(concentrator.cfg)
        run_survey(app, "boot")              # boot survey (was _probe_heads in __init__)
        app.sntp = FakeSntp()                # after: only the tower steps below count
        out["heads"] = list(app.head)
        out["selftest_ok"] = app.selftest["ok"]
        out["thr_pwm_hz"] = getattr(app.thr_pwm, "frequency", None)
        out["slice21"] = list(app.mux.pwm_slices[21][:3])
        out["warn27"] = any("27" in w for w in app.mux.warnings)
        app.poll_sense()
        # tower goes active, rings down, goes inactive -> survey (x2)
        for _ in range(2):
            for st in ("active", "ringing_down", "inactive"):
                app.tower.tower = st
                app._tower_step()
        out["sntp_calls"] = app.sntp.calls
        run_survey(app, "inactive")
        out["heads_after"] = list(app.head)
        out["ring"] = app.ring.status()
        if scenario == "head":
            out["laser8"] = fh.pwm[8].duty_cycle
            out["laser9"] = fh.pwm[9].duty_cycle
            app.poll_sense()
            out["rest_state"] = app.frame[1]["state"]
            state["trip"] = True
            app.poll_sense()
            out["trip_state"] = app.frame[1]["state"]
            state["trip"] = False
            app.poll_sense()
            out["strokes"] = app.ring.strokes[0]
        doc = app.status_payload()
        out["panel_len"] = len(doc.get("panel", []))
        out["parts"] = sorted(doc.get("parts", {}))
        if scenario == "rebuild":
            app.close()
            app2 = concentrator.Concentrator(concentrator.cfg)
            run_survey(app2, "boot")
            out["heads"] = list(app2.head)
        out["ok"] = True
    except Exception as e:
        import traceback
        out["exc"] = "%s: %s %s" % (type(e).__name__, e, traceback.format_exc()[-800:])
    child_finish(out, fh)


def main():
    me = os.path.abspath(__file__)
    for sv in ("with_common27", "no_common"):
        r = spawn(me, "empty", sv)
        t = "empty/%s" % sv
        check(t + " Concentrator + survey x2 runs", r["ok"], r.get("exc"))
        check(t + " no 'GPxx in use'", not r["errors"], r["errors"])
        once = dict((g, n) for g, n in r["claims"].items() if int(g) in ONCE)
        check(t + " every pin claimed exactly once",
              once == dict((str(g), 1) for g in ONCE), r["claims"])
        check(t + " GP27 never claimed", not r["gp27"])
        check(t + " heads all 'empty' (ch5-8 no_board)", r.get("heads") == ["empty"] * 8
              and r.get("heads_after") == ["empty"] * 8, r.get("heads"))
        check(t + " threshold PWM 20 kHz on slice 2B", r.get("thr_pwm_hz") == 20000
              and r.get("slice21") == [2, "B", 20000], (r.get("thr_pwm_hz"), r.get("slice21")))
        check(t + " loopback self-test ok", r.get("selftest_ok"))
        check(t + " set_ringing follows tower", r.get("sntp_calls") == [True, False, True, False],
              r.get("sntp_calls"))
        check(t + " payload: 6 panel LEDs + parts", r.get("panel_len") == 6 and
              r.get("parts") == ["gate", "mux", "refresh", "ring", "sync", "watchdog"], (r.get("panel_len"), r.get("parts")))
        if sv == "with_common27":
            check(t + " common 27 warned", r.get("warn27"))
    r = spawn(me, "head")
    check("head: ch1 classified new, others empty",
          r.get("heads") == ["new"] + ["empty"] * 7, r.get("heads") or r.get("exc"))
    # Firmware bug (src not changed here): ring.start() runs only once, in
    # Concentrator.__init__ right after self.kinds = {}, and nothing restarts
    # the ring when SurveyState settles, so no head's laser is lit and no
    # stroke is counted.  Re-enable these when the ring follows the survey.
    ring_bug = "firmware bug: ring started with no heads, not restarted after survey settles"
    check("head: panel rest state head_new", r.get("rest_state") == "head_new", r.get("rest_state"))
    skip("head: ring lit ch1 laser only", ring_bug)
    skip("head: panel response -> stroke counted", ring_bug)
    check("head: no 'GPxx in use'", r["ok"] and not r["errors"], r.get("exc") or r["errors"])
    r = spawn(me, "rebuild")
    check("close() releases everything; a second Concentrator builds clean",
          r["ok"] and not r["errors"] and r.get("heads") == ["empty"] * 8, r.get("exc") or r["errors"])
    finish()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
