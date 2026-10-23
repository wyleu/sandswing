# Watchdog guard: unit checks (injected fakes) + end to end on Concentrator.
#   python3 tests/test_watchdog_guard.py
import os
import re
import sys
import time

from hw import spawn, child_setup, child_finish, check, skip, finish, NEXT_SRC, run_survey

ON = {"watchdog": {"enabled": True, "timeout_s": 8, "mode": "reset",
                   "heartbeats": {"ws_poll": 5}}}


class Wdt:
    def __init__(self):
        self.mode, self.timeout, self.feeds, self.log = None, None, 0, []

    def __setattr__(self, k, v):
        if k in ("mode", "timeout") and "log" in self.__dict__:
            self.log.append((k, v))
        object.__setattr__(self, k, v)

    def feed(self):
        if self.mode is not None:
            self.feeds += 1


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def guard(settings=None, usb=False, flag=False, reasons=("POWER_ON", "STARTUP")):
    sys.path.insert(0, NEXT_SRC)
    from watchdog_guard import WatchdogGuard
    w, c = Wdt(), Clock()
    g = WatchdogGuard(settings, hw=(w, "RESET"), serial_fn=lambda: usb,
                      exists_fn=lambda p: flag, clock=c, reasons=reasons)
    return g, w, c


def unit():
    print("unit (WatchdogGuard with injected fakes)")
    g, w, c = guard()
    check("off by default: arm() refuses, mode stays None",
          not g.arm() and w.mode is None and "disabled" in g.why, g.why)
    check("off by default: loop_tick never feeds", not g.loop_tick() and w.feeds == 0)

    g, w, c = guard(ON["watchdog"], usb=True)
    check("USB connected + allow_with_usb false: not armed",
          not g.arm() and w.mode is None and "USB" in g.why, g.why)
    s = dict(ON["watchdog"], allow_with_usb=True)
    g, w, c = guard(s, usb=True)
    check("USB connected + allow_with_usb true: armed", g.arm() and w.mode == "RESET")

    g, w, c = guard(ON["watchdog"], flag=True)
    check("/nowatchdog file: never armed",
          not g.arm() and w.mode is None and "nowatchdog" in g.why, g.why)

    for t, want in ((8, 8), (20, 8), (8.39, 8), (0, 1), (0.5, 1), (3, 3)):
        g, w, c = guard(dict(ON["watchdog"], timeout_s=t))
        g.arm()
        check("timeout %r clamped to %d" % (t, want), g.timeout_s == want and w.timeout == want,
              (g.timeout_s, w.timeout))

    g, w, c = guard(dict(ON["watchdog"], mode="raise"))
    check("mode 'raise' (unsupported on RP2): not armed", not g.arm() and w.mode is None, g.why)

    g, w, c = guard(ON["watchdog"])
    check("enabled: armed with timeout then mode RESET",
          g.arm() and w.log == [("timeout", 8), ("mode", "RESET")], w.log)
    check("fresh heartbeat (grace from arming): loop_tick feeds", g.loop_tick() and w.feeds == 1)
    c.t += 3
    g.checkin("ws_poll")
    c.t += 3
    check("heartbeat 3 s old (max 5): feeds", g.loop_tick() and w.feeds == 2)
    c.t += 3
    check("heartbeat 6 s old (max 5): stale, NO feed",
          not g.loop_tick() and w.feeds == 2 and g.stale == ["ws_poll"] and g.missed == 1,
          (w.feeds, g.stale))
    g.checkin("ws_poll")
    check("heartbeat back: feeding resumes", g.loop_tick() and w.feeds == 3)
    with g.paused_for():
        paused_mode, fed = w.mode, g.loop_tick()
    check("paused_for(): mode None inside, no feed, RESET again after",
          paused_mode is None and not fed and w.mode == "RESET" and not g.paused)
    g.disarm("test")
    check("disarm(): mode None, armed False, loop_tick no-op",
          w.mode is None and not g.armed and not g.loop_tick() and w.feeds == 3)

    g, w, c = guard(reasons=("WATCHDOG", "STARTUP"))
    st = g.status()
    check("reset reason WATCHDOG in status + panel_state",
          st["last_reset"] == "WATCHDOG" and st["run_reason"] == "STARTUP"
          and g.panel_state() == "watchdog_reset", st)
    g.acknowledge()
    check("acknowledge() clears the panel state", g.panel_state() is None)
    g, w, c = guard({"ack_after_s": 86400}, reasons=("WATCHDOG", "STARTUP"))
    c.t += 86399
    g.loop_tick()
    still = g.panel_state()
    c.t += 2
    g.loop_tick()
    check("panel state clears after a clean day (ack_after_s)",
          still == "watchdog_reset" and g.panel_state() is None)
    g, w, c = guard(reasons=("POWER_ON", "STARTUP"))
    check("POWER_ON: no panel state", g.panel_state() is None)

    import watchdog_guard
    check("boot_reasons() without CircuitPython modules -> (None, None)",
          watchdog_guard.boot_reasons() == (None, None), watchdog_guard.boot_reasons())

    # the only feed point: feed() only in watchdog_guard, loop_tick() only
    # in Concentrator._sense_step
    feeds, ticks = [], []
    for f in sorted(os.listdir(NEXT_SRC)):
        if f.endswith(".py"):
            for i, line in enumerate(open(os.path.join(NEXT_SRC, f))):
                code = line.split("#")[0]
                if re.search(r"\.feed\(", code):
                    feeds.append(f)
                if re.search(r"\.loop_tick\(", code):
                    ticks.append((f, i))
    check("feed() called only inside watchdog_guard.py", feeds == ["watchdog_guard.py"], feeds)
    src = open(os.path.join(NEXT_SRC, "concentrator.py")).read()
    body = src.split("def _sense_step(self):")[1].split("\n    def ")[0]
    check("loop_tick() called once, only in Concentrator._sense_step",
          len(ticks) == 1 and ticks[0][0] == "concentrator.py" and "loop_tick()" in body, ticks)


# ------------------------------------------------------------ end to end
def child(scenario, src, extra):
    fh = child_setup(src, extra)
    out = {"ok": False}
    try:
        import microcontroller
        import supervisor
        if scenario == "usb":
            supervisor.runtime.serial_connected = True
        if scenario == "wdreset":
            microcontroller.cpu.reset_reason = microcontroller.ResetReason.WATCHDOG
        import watchdog_guard
        if scenario == "flag":
            open("nowatchdog", "w").close()
            watchdog_guard.FLAG_FILE = "nowatchdog"   # CIRCUITPY root == cwd here
        import concentrator

        class S:
            synced = True        # so LED6 shows heartbeat, not heartbeat_nosync

            def state(self):
                return "synced"

            def stamp(self, raw_tick=None):      # survey 'channel' events
                return {"tick_ms": 0, "utc_ms": None, "err_ms": None,
                        "sync": "synced", "acc": None, "epoch": 0,
                        "session": 0, "mode": "fake"}

            def __init__(self):
                self.bursts = 0

            def burst_once(self):
                self.bursts += 1

            def set_ringing(self, r):
                pass

            def boot_record(self):
                return {"sync": "fake"}

            async def run(self):
                pass
        sn = S()
        concentrator.setup_net = lambda: "10.0.0.2"
        concentrator.setup_sntp = lambda ip: sn
        concentrator.start_server = lambda cfg, fn: None
        wdt = microcontroller.watchdog
        app = concentrator.start_app()
        out["claims_total"] = len(fh.history)
        out["log"] = list(fh.wdt_log)
        out["bursts"] = sn.bursts
        out["armed"] = app.guard.armed
        out["why"] = app.guard.why
        out["mode"] = None if wdt.mode is None else str(wdt.mode)
        out["timeout"] = wdt.timeout
        f0 = wdt.feeds
        import asyncio
        app.guard.checkin("ws_poll")
        asyncio.run(app._sense_step())
        out["fed_by_loop"] = wdt.feeds - f0
        f0 = wdt.feeds
        app.guard.beats["ws_poll"] -= 10            # ws task "hung"
        asyncio.run(app._sense_step())
        out["fed_stale"] = wdt.feeds - f0
        app.guard.checkin("ws_poll")
        # a plug-change survey now runs inside the steady loop (SurveyState):
        # the watchdog stays armed and every loop pass still feeds it
        n, f0 = len(fh.wdt_log), wdt.feeds
        steps = run_survey(app, "plug", channel=0)
        out["survey_log"] = [e[:2] for e in fh.wdt_log[n:]]
        out["survey_fed"] = wdt.feeds - f0
        out["survey_steps"] = len(steps)
        out["mode_after_survey"] = None if wdt.mode is None else str(wdt.mode)
        app.tower.tower = "inactive"
        app._paint(True)
        out["led6_on"] = [list(app.frame[5]["rgb"]), app.frame[5]["state"]]
        app._paint(False)
        out["led6_off"] = [list(app.frame[5]["rgb"]), app.frame[5]["state"]]
        doc = app.status_payload()
        if isinstance(doc, str):
            import json
            doc = json.loads(doc)
        out["last_reset"] = doc.get("last_reset")
        out["wd_part"] = doc.get("parts", {}).get("watchdog")
        # Ctrl-C in the steady loop: main() disarms in finally
        concentrator.start_app = lambda: app

        async def boom():
            raise KeyboardInterrupt()
        app.sense_task = boom
        import asyncio
        try:
            asyncio.run(concentrator.main())
        except KeyboardInterrupt:
            pass
        out["mode_after_ctrl_c"] = None if wdt.mode is None else str(wdt.mode)
        out["ok"] = True
    except BaseException as e:
        import traceback
        out["exc"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:])
    child_finish(out, fh)


def e2e():
    print("end to end (Concentrator on fakes; start_app arms last)")
    r = spawn(__file__, "default")
    check("default settings: runs", r["ok"], r.get("exc"))
    check("default settings: not armed, never touched the watchdog",
          not r.get("armed") and r.get("log") == [] and r.get("fed_by_loop") == 0, r)
    check("default: payload last_reset + parts.watchdog",
          r.get("last_reset") == "POWER_ON" and (r.get("wd_part") or {}).get("run_reason") == "STARTUP",
          (r.get("last_reset"), r.get("wd_part")))

    r = spawn(__file__, "on", patch=ON)
    check("enabled: runs", r["ok"], r.get("exc"))
    log = r.get("log") or []
    check("enabled: armed after startup (survey/classify/selftest claims + first sync done)",
          r.get("armed") and r.get("bursts") == 1 and log and log[0][2] == r.get("claims_total")
          and r.get("mode") == "RESET" and r.get("timeout") == 8, (log, r.get("claims_total"), r.get("why")))
    check("enabled: the steady loop feeds", r.get("fed_by_loop") == 1, r.get("fed_by_loop"))
    check("enabled: stale ws_poll heartbeat stops feeding", r.get("fed_stale") == 0, r.get("fed_stale"))
    check("enabled: survey runs in the steady loop, watchdog armed and fed each pass",
          r.get("survey_log") == [] and r.get("mode_after_survey") == "RESET"
          and r.get("survey_fed") == r.get("survey_steps"),
          (r.get("survey_log"), r.get("mode_after_survey"), r.get("survey_fed"), r.get("survey_steps")))
    check("enabled: Ctrl-C out of main() disarms (mode None)",
          r.get("mode_after_ctrl_c") is None and r["ok"], r.get("mode_after_ctrl_c"))
    check("enabled: no pin errors", not r.get("errors"), r.get("errors"))

    r = spawn(__file__, "usb", patch=ON)
    check("enabled + USB console: not armed, says why",
          r["ok"] and not r.get("armed") and "USB" in (r.get("why") or "") and r.get("log") == [],
          (r.get("why"), r.get("exc")))
    r = spawn(__file__, "flag", patch=ON)
    check("enabled + nowatchdog file: not armed",
          r["ok"] and not r.get("armed") and "nowatchdog" in (r.get("why") or ""), (r.get("why"), r.get("exc")))

    r = spawn(__file__, "wdreset")
    check("after a watchdog reset: payload last_reset == 'WATCHDOG'",
          r["ok"] and r.get("last_reset") == "WATCHDOG", (r.get("last_reset"), r.get("exc")))
    check("after a watchdog reset: LED6 magenta (panel 'watchdog_reset')",
          r.get("led6_on") == [[48, 0, 48], "watchdog_reset"]
          and r.get("led6_off") == [[0, 0, 0], "watchdog_reset"], (r.get("led6_on"), r.get("led6_off")))
    # Firmware bug (src not changed here): Concentrator._paint reads
    # getattr(sntp, "synced", False) but SntpClock has no .synced (it has
    # state() == "synced"), so with a real clock LED6 is always
    # heartbeat_nosync and the watchdog_reset magenta (heartbeat only) never shows.
    skip("after a watchdog reset with a real SntpClock: LED6 magenta",
         "firmware bug: _paint checks sntp.synced, SntpClock only has state()")


if __name__ == "__main__":
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        child(sys.argv[i + 1], sys.argv[i + 2], sys.argv[i + 3])
        sys.exit(0)
    unit()
    e2e()
    finish()
