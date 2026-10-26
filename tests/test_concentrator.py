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

    sync_state = "synced"
    server, stratum, last, peer = "10.42.0.1", 1, None, None

    def state(self):
        return self.sync_state

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


def link(app):
    """Heartbeat frame, LED holdover/nosync, Wi-Fi loss -> rejoin -> back,
    and the silent-link radio reset (fake farm_ws functions)."""
    import concentrator
    import farm_ws
    sent = []
    concentrator.send = lambda obj, keep=True: sent.append((obj, keep))
    app.sntp = FakeSntp()
    app.tower.tower = "inactive"
    res = {}
    for st in ("synced", "stale", "unsynced"):
        app.sntp.sync_state = st
        app._paint(True)
        res["led6_" + st] = [list(app.frame[5]["rgb"]), app.frame[5]["state"]]
    app._paint(False)                              # dark half of the blink
    hb = app.heartbeat_doc()
    res["hb"] = {"schema": hb["schema"], "type": hb["type"], "seq": hb["seq"],
                 "keys": sorted(k for k in hb if k != "body"),
                 "led6": hb["body"]["panel"][5], "sync": hb["body"]["sync"],
                 "has": sorted(k for k in ("panel", "panel_state", "link", "net",
                                           "time", "tower", "last_seq")
                               if k in hb["body"])}
    # Wi-Fi lost -> link event, LED1 no_wifi; rejoin after WIFI_REJOIN_S
    wifi = {"ok": True}
    joins, started, drops, resets = [], [], [], []
    farm_ws.wifi_ok = lambda: wifi["ok"]
    farm_ws.wifi_ip = lambda: "10.42.0.81"
    farm_ws.drop_all = drops.append
    farm_ws.connect_wifi = lambda cfg, timeout=None: joins.append(timeout)
    farm_ws.server_running = lambda: bool(started)
    farm_ws.reset_radio = lambda: resets.append(1)
    concentrator.start_server = lambda cfg, fn: started.append(1)
    app.wifi_up, app.link = True, "ws_client"
    state = {"lost_at": None, "tried_at": 0.0}
    wifi["ok"] = False
    app.link_step(100.0, state)
    res["lost"] = [app.link, app.wifi_lost, list(drops), list(joins)]
    app.link_step(100.0 + concentrator.WIFI_REJOIN_S - 1, state)
    res["early_joins"] = len(joins)
    app.link_step(100.0 + concentrator.WIFI_REJOIN_S, state)
    res["joins"] = list(joins)
    wifi["ok"] = True
    app.link_step(140.0, state)
    res["back"] = [app.link, app.wifi_up, len(started)]
    # Wi-Fi "up" but nothing heard: one radio reset per silent spell
    farm_ws.stats["hb_seen"] = True
    farm_ws.last_rx_age_s = lambda now=None: concentrator.LINK_DEAD_S + 1
    app.link_step(200.0, state)
    app.link_step(202.0, state)
    res["radio_resets"] = len(resets)
    whats = [o["body"]["what"] for o, k in sent if o.get("type") == "link"]
    res["link_whats"] = whats
    res["link_kept"] = all(k for o, k in sent if o.get("type") == "link")
    # Gateway silent while no client is connecting: radio reset after
    # GATEWAY_FAIL_N misses, but never if it has not answered since boot.
    farm_ws.stats["hb_seen"] = False
    gw = {"ok": None}
    farm_ws.gateway_ping = lambda timeout=1.0: gw["ok"]
    app._dead_handled = False
    app.link_health = concentrator.LinkHealth()   # a fresh spell (the Pico would have rebooted)
    app._gw_since = None
    del resets[:]
    t = 1000.0
    def gstep():
        nonlocal t
        t += concentrator.GATEWAY_CHECK_S
        app.link_step(t, state)
    gw["ok"] = False
    for _ in range(concentrator.GATEWAY_FAIL_N + 1):
        gstep()
    res["gw_unseen_resets"] = len(resets)       # never answered: no rule
    gw["ok"] = True
    gstep()
    gw["ok"] = False
    for _ in range(concentrator.GATEWAY_FAIL_N):
        gstep()
    res["gw_resets"] = len(resets)
    gw["ok"] = True
    gstep()
    res["gw_fail_cleared"] = app._gw_fail
    # heartbeat_task logs when the loop ran late (it once crashed main(): len(int))
    import asyncio
    import types
    clock = {"now": 100.0}
    stub = types.SimpleNamespace(monotonic=lambda: clock["now"])
    calls = []

    class _Stop(Exception):
        pass

    async def fake_sleep(s):
        calls.append(s)
        if len(calls) > 1:
            raise _Stop()
        clock["now"] += s + 3.0                    # the loop was held 3 s

    notes = []
    saved = (concentrator.time, concentrator.asyncio.sleep, concentrator.emit,
             farm_ws.ws_clients)
    concentrator.time, concentrator.asyncio.sleep = stub, fake_sleep
    concentrator.emit = notes.append
    farm_ws.ws_clients = lambda: 1
    try:
        asyncio.run(app.heartbeat_task())
    except _Stop:
        pass
    finally:
        (concentrator.time, concentrator.asyncio.sleep, concentrator.emit,
         farm_ws.ws_clients) = saved
    res["loop_late"] = notes
    return {"link": res}


def plug_trigger(fh, pressed, to_ground=False):
    """ch1 = trigger plug: Orange <-> Green/White strapped (GP28 loaded,
    GP26 follows it when ch1 is selected); Green -> Blue/White only while
    pressed["on"].  Open: Blue/White idles high (board pull-up)."""
    import mux_select

    def sel0():
        t = list(mux_select._TOWERS.values())[0]
        return t.current == 0

    def gp28():
        d = fh.pwm[21].duty_cycle if 21 in fh.pwm else 0
        return int(d * (0.835 if sel0() else 1.0))

    def gp26():
        return int(gp28() * 0.99) if sel0() else 700

    def bw():
        if pressed["on"]:
            if to_ground:
                return False                                  # 0 V
            return 8 in fh.pwm and fh.pwm[8].duty_cycle > 32767   # = Green
        return True
    fh.adc_fn[28] = gp28
    fh.adc_fn[26] = gp26
    fh.din_fn[0] = bw


def trigger(app, fh, pressed):
    """Trigger plug on ch1: classified 'trigger', a press is one labelled
    stroke on the normal path, bounce is ignored, presses never resurvey."""
    import concentrator
    from sntp_pico import TickClock
    res = {"heads": list(app.head), "laser8": fh.pwm[8].duty_cycle}
    t = [5000000]
    app.ticks = TickClock(lambda: t[0])
    sent = []
    concentrator.send = lambda obj, keep=True: sent.append(obj)
    app.sntp = SessionSntp()
    app.tower.tower = "inactive"

    def sample(on, dt_ms):
        t[0] += dt_ms
        pressed["on"] = on
        app.poll_sense()
    sample(False, 20)
    sample(False, 60)                     # open >= TRIGGER_REARM_MS: armed
    paint_once(app)
    res["rest_led"] = [list(app.frame[1]["rgb"]), app.frame[1]["state"]]
    sample(True, 20)                      # press 1
    paint_once(app)
    res["down_led"] = app.frame[1]["state"]
    sample(False, 5)                      # press bounce
    sample(True, 5)
    sample(True, 300)                     # held
    sample(False, 20)                     # release ...
    sample(True, 10)                      # ... release bounce (not re-armed)
    sample(False, 10)
    sample(False, 60)                     # clean release: armed
    sample(True, 20)                      # press 2
    sample(False, 60)
    sample(False, 60)
    for _ in range(5):                    # quick presses, 4 Hz
        sample(True, 125)
        sample(False, 125)
    res["doubt"] = app.survey_state.status()["doubt"]
    res["strokes"] = [e["body"] for e in sent if e.get("type") == "stroke"]
    res["types"] = sorted(set(e.get("type") for e in sent))
    # held for ages with the tower quiet: one resurvey request
    app.tower.tower = "inactive"
    sample(True, 20)
    for _ in range(3):
        app.tower.tower = "inactive"
        sample(True, concentrator.TRIGGER_STUCK_MS // 2)
    res["stuck_doubt"] = app.survey_state.status()["doubt"]
    res["stuck_ch"] = getattr(app.survey_state, "_pending_ch", None)
    pressed["on"] = False
    return {"trigger": res}


def plug_orange_short(fh, ch):
    """Orange on channel ch shorted to 0 V: GP28 reads ~0 when it is selected."""
    import mux_select

    def gp28():
        t = list(mux_select._TOWERS.values())[0]
        if t.current == ch:
            return 0
        return fh.pwm[21].duty_cycle if 21 in fh.pwm else 0
    fh.adc_fn[28] = gp28


def why(app, fh, sent, lines):
    """Survey why: console lines, channel events on change only, /status
    parts.survey (with what latched a fault), heartbeat chan brief."""
    res = {"boot_events": [e["body"] for e in sent if e.get("type") == "channel"],
           "boot_lines": [l for l in lines if l.startswith("ch ")]}
    st = app.status_payload()["parts"]["survey"]
    res["status_ch2"] = st["channels"][1]
    res["fault_seq"] = [e["seq"] for e in sent if e.get("type") == "channel"
                        and e["body"]["ch"] == 2][0]
    res["hb_chan"] = app.heartbeat_doc()["body"]["chan"][:3]
    doc = app.status_payload()
    res["panels"] = [x["id"] for x in doc["sections"]]
    res["panel_ch2"] = [i for x in doc["sections"] if x["id"] == "heads"
                        for i in x["items"] if i["key"] == "ch2_why"]
    del sent[:], lines[:]
    fh.adc_fn.pop(28, None)                  # short removed
    run_survey(app, "plug", channel=1)
    res["fix_events"] = [e["body"] for e in sent if e.get("type") == "channel"]
    res["fix_lines"] = [l for l in lines if l.startswith("ch 2 ")]
    res["fix_latched"] = app.status_payload()["parts"]["survey"]["channels"][1]["latched"]
    return {"why": res}


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
        pressed = {"on": scenario in ("trigger_held", "trigger_held_gnd")}
        if scenario == "head":
            plug_head()
        if scenario in ("trigger", "trigger_held", "trigger_held_gnd"):
            plug_trigger(fh, pressed, to_ground=scenario == "trigger_held_gnd")
        import concentrator
        sent, lines = [], []
        if scenario == "why":
            plug_orange_short(fh, 1)
            concentrator.send = lambda obj, keep=True: sent.append(obj)
            real_emit = concentrator.emit

            def emit(msg):
                lines.append(msg)
                real_emit(msg)
            concentrator.emit = emit
        app = concentrator.Concentrator(concentrator.cfg)
        run_survey(app, "boot")              # boot survey (was _probe_heads in __init__)
        if scenario == "strokes":
            out.update(strokes(app, fh))
        if scenario == "watch":
            out.update(watch_empty(app, fh, plug_head))
        if scenario == "link":
            out.update(link(app))
        if scenario == "trigger":
            out.update(trigger(app, fh, pressed))
        if scenario == "why":
            out.update(why(app, fh, sent, lines))
        if scenario == "trigger_held_gnd":
            import concentrator as C
            out["gnd_heads"] = list(app.head)
            sent = []
            C.send = lambda obj, keep=True: sent.append(obj)
            pressed["on"] = False                    # release: no stroke, no survey
            app.poll_sense()
            out["gnd_release_doubt"] = app.survey_state.status()["doubt"]
            out["gnd_release_strokes"] = len(sent)
        if scenario == "trigger_held":
            out["held_heads"] = list(app.head)
            pressed["on"] = False                    # released after the survey
            app.poll_sense()
            out["held_release_doubt"] = app.survey_state.status()["doubt"]
            run_survey(app, "plug", channel=0)
            out["held_after"] = list(app.head)
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
              r.get("parts") == ["gate", "link", "mux", "refresh", "ring", "survey", "sync", "watchdog"], (r.get("panel_len"), r.get("parts")))
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
    r = spawn(me, "trigger")
    T = r.get("trigger") or {}
    check("trigger: ch1 classified 'trigger', others empty, laser held at 0",
          r["ok"] and T.get("heads") == ["trigger"] + ["empty"] * 7
          and T.get("laser8") == 0, r.get("exc") or (T.get("heads"), T.get("laser8")))
    check("trigger: violet at rest, white while pressed",
          T.get("rest_led") == [[24, 0, 48], "trigger"] and T.get("down_led") == "response",
          (T.get("rest_led"), T.get("down_led")))
    st = T.get("strokes") or []
    check("trigger: each press one labelled stroke; press/release bounce ignored",
          len(st) == 7 and all(b == {"ch": 1, "test": True, "src": "trigger"} for b in st), st)
    check("trigger: presses never ask for a resurvey", T.get("doubt") is False, T.get("doubt"))
    check("trigger: only stroke events sent", T.get("types") == ["stroke"], T.get("types"))
    check("trigger: held > TRIGGER_STUCK_MS (tower quiet) -> one resurvey of ch1",
          T.get("stuck_doubt") is True and T.get("stuck_ch") == 0,
          (T.get("stuck_doubt"), T.get("stuck_ch")))
    check("trigger: self-test still ok with a trigger plug", r.get("selftest_ok"), r.get("selftest_ok"))
    r = spawn(me, "trigger_held")
    check("trigger held through the survey: loopback, release resurveys -> trigger",
          r["ok"] and (r.get("held_heads") or [None])[0] == "lb"
          and r.get("held_release_doubt") is True
          and (r.get("held_after") or [None])[0] == "trigger",
          r.get("exc") or (r.get("held_heads"), r.get("held_release_doubt"), r.get("held_after")))
    r = spawn(me, "trigger_held_gnd")
    check("trigger (switch to 0 V) held through the survey: still 'trigger'; "
          "release sends nothing and does not resurvey",
          r["ok"] and (r.get("gnd_heads") or [None])[0] == "trigger"
          and r.get("gnd_release_doubt") is False and r.get("gnd_release_strokes") == 0,
          r.get("exc") or (r.get("gnd_heads"), r.get("gnd_release_doubt"),
                           r.get("gnd_release_strokes")))
    r = spawn(me, "why")
    W = r.get("why") or {}
    be = W.get("boot_events") or []
    check("why: boot survey stores one 'channel' event per channel (prev unknown)",
          r["ok"] and len(be) == 8 and all(b["prev_kind"] == "unknown" for b in be)
          and [b["ch"] for b in be] == list(range(1, 9)), r.get("exc") or be)
    f = [b for b in be if b["ch"] == 2]
    check("why: ch2 Orange short -> fault, why carries the measurement",
          f and f[0]["kind"] == "fault" and "(l50 0.00 < 0.20)" in f[0]["why"][0], f)
    bl = W.get("boot_lines") or []
    check("why: one console line per channel, e.g. 'ch 2 fault: ... (was unknown)'",
          len(bl) == 8 and bl[1].startswith("ch 2 fault: Orange shorted to ground")
          and bl[1].endswith("(was unknown)"), bl[:2])
    s2 = W.get("status_ch2") or {}
    check("why: /status parts.survey ch2 kind + why + latched (seq, uptime)",
          s2.get("kind") == "fault" and s2.get("why") and (s2.get("latched") or {}).get("seq")
          == W.get("fault_seq") and "uptime_s" in (s2.get("latched") or {}), s2)
    hb = W.get("hb_chan") or []
    check("why: heartbeat chan = [kind, short why] (why only for fault/trigger)",
          hb[:1] == [["empty", None]] and hb[1][0] == "fault"
          and hb[1][1].startswith("Orange shorted") and len(hb[1][1]) <= 80
          and hb[1][1].endswith("... (l50 0.00 < 0.20)"), hb)
    check("status panels: network, time, heads, tower, concentrator; fault why shown",
          W.get("panels") == ["network", "time", "heads", "tower", "concentrator"]
          and len(W.get("panel_ch2") or []) == 1, (W.get("panels"), W.get("panel_ch2")))
    fe = W.get("fix_events") or []
    check("why: resurvey stores an event only for the changed channel",
          fe == [{"ch": 2, "kind": "empty", "prev_kind": "fault", "why": fe[0]["why"]}]
          if fe else False, fe)
    check("why: changed channel's console line says (was fault); latch cleared",
          (W.get("fix_lines") or [""])[-1].endswith("(was fault)")
          and W.get("fix_latched") is None, (W.get("fix_lines"), W.get("fix_latched")))
    r = spawn(me, "link")
    L = r.get("link") or {}
    check("link: LED6 white synced / amber holdover / red unsynced",
          L.get("led6_synced") == [[8, 8, 8], "heartbeat"]
          and L.get("led6_stale") == [[40, 20, 0], "heartbeat_holdover"]
          and L.get("led6_unsynced") == [[40, 0, 0], "heartbeat_nosync"], r.get("exc") or L)
    hb = L.get("hb") or {}
    check("link: a held loop is logged with phase and client count (no crash)",
          len(L.get("loop_late") or []) == 1
          and (L["loop_late"][0] if L.get("loop_late") else "").startswith("loop late 3.0 s")
          and "clients 1" in L["loop_late"][0], L.get("loop_late") or r.get("exc"))
    check("link: heartbeat is sand.panel/v1 with the event envelope",
          hb.get("schema") == "sand.panel/v1" and hb.get("type") == "heartbeat"
          and hb.get("seq") == 1 and {"src", "id", "boot_id", "seq", "tick_ms",
                                      "utc_ms", "sync"} <= set(hb.get("keys", [])), hb)
    check("link: heartbeat LED mimic is the ON phase, with link/net/time",
          hb.get("led6") == [40, 0, 0] and hb.get("has") == sorted(
              ["panel", "panel_state", "link", "net", "time", "tower", "last_seq"]), hb)
    check("link: Wi-Fi lost -> LED1 no_wifi, clients dropped, no instant rejoin",
          L.get("lost") == ["no_wifi", 1, ["wifi lost"], []], L.get("lost"))
    check("link: rejoin only after WIFI_REJOIN_S, with a join timeout",
          L.get("early_joins") == 0 and L.get("joins") == [10], (L.get("early_joins"), L.get("joins")))
    check("link: back -> wifi_only, server restarted",
          L.get("back") == ["wifi_only", True, 1], L.get("back"))
    check("link: gateway silent -> radio reset, never without a first answer",
          L.get("gw_unseen_resets") == 0 and L.get("gw_resets") == 1
          and L.get("gw_fail_cleared") == 0,
          [L.get("gw_unseen_resets"), L.get("gw_resets"), L.get("gw_fail_cleared")])
    check("link: silent link -> one radio reset per spell", L.get("radio_resets") == 1,
          L.get("radio_resets"))
    check("link: link events wifi_lost, wifi_back, link_dead (kept for replay)",
          L.get("link_whats") == ["wifi_lost", "wifi_back", "link_dead"] and L.get("link_kept"),
          L.get("link_whats"))
    r = spawn(me, "rebuild")
    check("close() releases everything; a second Concentrator builds clean",
          r["ok"] and not r["errors"] and r.get("heads") == ["empty"] * 8, r.get("exc") or r["errors"])
    finish()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
