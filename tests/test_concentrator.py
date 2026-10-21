# End to end: construct Concentrator on the fakes, survey (SurveyState), ring.
import os
import sys
from hw import spawn, child_setup, child_finish, check, finish, run_survey, paint_once

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

class SessionSntp(FakeSntp):
    """set_ringing(True) on inactive->active starts a new session, like
    SntpClock; stamp() reports session and mode."""
    def __init__(self):
        FakeSntp.__init__(self)
        self.ringing, self.session, self.wakes = False, 0, 0

    def set_ringing(self, r):
        if r != self.ringing:
            self.ringing = r
            if r:
                self.session += 1
                self.wakes += 1

    def stamp(self, raw_tick=None):
        st = FakeSntp.stamp(self, raw_tick)
        st.update(session=self.session, mode="ringing" if self.ringing else "free")
        return st


DOUBLE_MS = 85            # bench: ch2 EDGEs 85 ms apart
AFTER_GAP_MS = 300        # > MIN_STROKE_GAP_MS
OTHER_CH_MS = 50          # ch2 stroke this soon after ch1's


def strokes(app, fh):
    """Waking stroke session/mode and the minimum stroke gap (int ms ticks)."""
    import concentrator
    from sntp_pico import TickClock
    level = {0: True, 1: True}                   # True = beam clear (not held)
    fh.din_fn[0] = lambda: level[0]
    fh.din_fn[1] = lambda: level[1]
    app.head[0] = app.head[1] = "new"            # heads, not plug-change sockets
    t = [1000000]
    app.ticks = TickClock(lambda: t[0])
    app.ring.clock_ms = app.ticks.now
    sent = []
    concentrator.send = sent.append
    app.sntp = SessionSntp()
    app.poll_sense()                             # all clear: last_false set
    app.tower.poll()
    app._tower_step()
    tower0 = app.tower.tower

    def stroke(ch, dt_ms):
        t[0] += dt_ms
        level[ch] = False
        app.poll_sense()
        level[ch] = True
        app.poll_sense()
    stroke(0, 0)                                 # waking stroke
    stroke(0, DOUBLE_MS)                         # double: ignored
    stroke(0, AFTER_GAP_MS)                      # next stroke
    stroke(1, OTHER_CH_MS)                       # other channel: independent
    app._tower_step()
    return {"tower0": tower0, "wakes": app.sntp.wakes,
            "events": [[e["body"]["ch"], e["session"], e["mode"]] for e in sent]}


def watch_empty(app, fh, plug_head):
    """_watch_empty with a head now in empty ch1: no pulse / select while
    the tower rings or a survey runs; triggers 'empty-head' when inactive."""
    import asyncio
    import concentrator
    lasers = set(concentrator.load_pinmap(concentrator.cfg)["laser_gp"])
    selects = []
    real_select = app.mux.select

    def counting_select(b):
        selects.append(b)
        return real_select(b)
    app.mux.select = counting_select
    plug_head()
    res = {}

    def check_once(name):
        del selects[:]
        del fh.duty_log[:]
        now = app._empty_check + concentrator.EMPTY_CHECK_S
        asyncio.run(app._watch_empty(now))
        res[name] = {"selects": len(selects),
                     "pulses": len([1 for gp, d in fh.duty_log if gp in lasers and d]),
                     "doubt": app.survey_state.status()["doubt"],
                     "ch": getattr(app.survey_state, "_pending_ch", None)}
    for st in ("active", "ringing_down", "inactive"):
        app.tower.tower = st
        check_once(st)
    asyncio.run(app.survey_state.step(True))       # survey now running
    res["survey_phase"] = app.survey_state.phase
    check_once("surveying")
    return {"watch": res}


def child(scenario, src, extra):
    fh = child_setup(src, extra)
    out = {"ok": False}
    state = {"trip": False}

    def plug_head():
        # ch1 = a new head: laser current on GP26 when selected and lit;
        # Blue/White follows the threshold (LM393) and is high at rest.
        import mux_select

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

    try:
        if scenario == "head":
            plug_head()
        import concentrator
        app = concentrator.Concentrator(concentrator.cfg)
        run_survey(app, "boot")              # boot survey (was _probe_heads in __init__)
        if scenario == "strokes":
            out.update(strokes(app, fh))
        if scenario == "watch":
            out.update(watch_empty(app, fh, plug_head))
        if scenario == "late":
            # empty at boot; a head is plugged into ch1 later
            out["laser8_boot"] = fh.pwm[8].duty_cycle
            plug_head()
            run_survey(app, "empty-head", channel=0)
            out["heads_late"] = list(app.head)
            out["laser8_late"] = fh.pwm[8].duty_cycle
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
            paint_once(app)
            out["rest_state"] = app.frame[1]["state"]
            state["trip"] = True
            app.poll_sense()
            paint_once(app)
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
    check("head: ring lit ch1 laser only", r.get("laser8") == 65535 and r.get("laser9") == 0,
          (r.get("laser8"), r.get("laser9")))
    check("head: panel rest -> response -> stroke counted",
          r.get("rest_state") == "head_new" and r.get("trip_state") == "response"
          and r.get("strokes") == 1, (r.get("rest_state"), r.get("trip_state"), r.get("strokes")))
    check("head: no 'GPxx in use'", r["ok"] and not r["errors"], r.get("exc") or r["errors"])
    r = spawn(me, "late")
    check("late head: a later survey finds ch1 and re-lights the ring",
          r["ok"] and r.get("laser8_boot") == 0 and r.get("heads_late") == ["new"] + ["empty"] * 7
          and r.get("laser8_late") == 65535,
          r.get("exc") or (r.get("laser8_boot"), r.get("heads_late"), r.get("laser8_late")))
    r = spawn(me, "strokes")
    ev = r.get("events") or []
    check("strokes: tower inactive before the waking stroke", r.get("tower0") == "inactive",
          r.get("exc") or r.get("tower0"))
    check("strokes: waking stroke carries the new session, mode ringing",
          ev[:1] == [[1, 1, "ringing"]], ev)
    check("strokes: %d ms double ignored, stroke after the gap accepted" % DOUBLE_MS,
          [e[0] for e in ev] == [1, 1, 2], ev)
    check("strokes: other channel independent of ch1's gap", ev[-1:] == [[2, 1, "ringing"]], ev)
    check("strokes: one session for the whole ring (one wake)",
          r.get("wakes") == 1 and all(e[1] == 1 for e in ev), (r.get("wakes"), ev))
    r = spawn(me, "watch")
    w = r.get("watch") or {}
    quiet = {"selects": 0, "pulses": 0, "doubt": False, "ch": None}
    for st in ("active", "ringing_down"):
        check("empty check: no laser pulse or mux select while tower " + st,
              r["ok"] and w.get(st) == quiet, r.get("exc") or w.get(st))
    check("empty check: inactive + idle pulses ch1 and triggers 'empty-head' ch1",
          (w.get("inactive") or {}).get("pulses", 0) > 0
          and (w.get("inactive") or {}).get("selects", 0) > 0
          and w["inactive"]["doubt"] and w["inactive"]["ch"] == 0, w.get("inactive"))
    check("empty check: no laser pulse or mux select while a survey runs",
          w.get("survey_phase") == "examining"
          and (w.get("surveying") or {}).get("selects") == 0
          and (w.get("surveying") or {}).get("pulses") == 0, (w.get("survey_phase"), w.get("surveying")))
    r = spawn(me, "rebuild")
    check("close() releases everything; a second Concentrator builds clean",
          r["ok"] and not r["errors"] and r.get("heads") == ["empty"] * 8, r.get("exc") or r["errors"])
    finish()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
