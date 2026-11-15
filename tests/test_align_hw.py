# End to end: the real Concentrator on the fake CircuitPython modules, a new
# head on ch1, the sandheads scope driven the way align.js drives it (through
# Aligner.on_message, as farm_ws's hook would).  Strokes must still count and
# go out once, the trace must follow the sense pin, nudges must reach the
# laser PWM / the refresher, GP27 gives ch1's PT level, settings.json is not
# written.
import json
import os
import sys
from hw import spawn, child_setup, child_finish, check, finish, run_survey


def child(scenario, src, extra):
    fh = child_setup(src, extra)
    out = {"ok": False}
    state = {"trip": False}
    try:
        import mux_select

        def cur():
            t = list(mux_select._TOWERS.values())[0]
            lit = 8 in fh.pwm and fh.pwm[8].duty_cycle == 65535
            return 700 + (1490 if (lit and t.current == 0) else 0)

        def bw():
            if state["trip"]:
                return False
            return not (21 in fh.pwm and fh.pwm[21].duty_cycle > 49152)
        fh.adc_fn[26] = cur
        fh.adc_fn[27] = lambda: 31000                # head 5's Orange/White
        fh.din_fn[0] = bw
        import asyncio
        import concentrator
        import align_scope as A
        sent, mine = [], []
        concentrator.send = lambda obj, keep=True, udp_fit=None: sent.append(
            (json.loads(json.dumps(obj)), keep))
        before = open("settings.json", "rb").read()
        app = concentrator.Concentrator(concentrator.cfg)
        run_survey(app, "boot")
        out["heads"] = list(app.head)
        out["has_align"] = app.align is not None
        out["pt_ch"] = None if app.pt_level is None else app.pt_level.ch

        def send_to(cid, obj):
            mine.append((cid, json.loads(json.dumps(obj))))
            return True
        app.align.send_to = send_to

        def say(**kw):
            d = {"cmd": "align"}
            d.update(kw)
            app.align.on_message(5, json.dumps(d))
            app.align.tick()
            acks = [o for c, o in mine if o["type"] == "align_ack"]
            return acks[-1]["body"] if acks else None

        def step(n=1):
            for _ in range(n):
                asyncio.run(app._sense_step())
                app.align.tick()

        app.tower.tower = "inactive"
        step(3)
        out["idle_samples"] = app.align.ring.count
        app.align.on_message(5, '{"cmd":"align","info":true}')
        app.align.tick()
        info = [o for c, o in mine if o["type"] == "align_info"]
        out["info"] = info[-1]["body"] if info else None
        out["scope"] = say(ch=1, scope=True)
        t = [app.ticks.now()]

        def adv():
            t[0] += 20
            return t[0]
        app.align.clock = adv
        strokes0 = app.ring.strokes[0]
        step(10)
        state["trip"] = True
        step(2)
        state["trip"] = False
        step(25)
        frames = [o for c, o in mine if o["type"] == "align"]
        out["n_frames"] = len(frames)
        out["to_one_client"] = all(c == 5 for c, o in mine)
        out["broadcast_align"] = [o for o, k in sent if str(o.get("type", "")).startswith("align")]
        if frames:
            b = frames[-1]["body"]
            out["body"] = b
            out["bits"] = A.unpack_bits(b["bits"], b["n"]) if b["n"] else []
        out["strokes"] = app.ring.strokes[0] - strokes0
        out["stroke_events"] = len([o for o, k in sent if o.get("type") == "stroke"])
        app.tower.tower = "inactive"
        t[0] += 1000
        out["thr"] = say(ch=1, thr=+1)
        t[0] += 1000
        out["drive"] = say(ch=1, drive=-1)
        out["laser8_after"] = fh.pwm[8].duty_cycle
        out["target"] = dict(app.refresher.target)
        out["default"] = app.thr_settings["default_duty"]
        out["steps"] = [app.tune.settings["threshold"]["step"], app.tune.settings["laser"]["step"]]
        run_survey(app, "plug", channel=0)
        out["laser8_resurvey"] = fh.pwm[8].duty_cycle
        app.tower.tower = "active"
        t[0] += 1000
        out["refused"] = say(ch=1, drive=-1)
        out["laser8_refused"] = fh.pwm[8].duty_cycle
        app.tower.tower = "inactive"
        out["ch2"] = say(ch=2, scope=True)
        out["tune_get"] = app.tune.handle("tune_get", 0, None, None)
        out["tune_get2"] = app.tune.handle("tune_get", 1, None, None)
        out["settings_same"] = open("settings.json", "rb").read() == before
        out["parts"] = sorted(app.status_payload().get("parts", {}))
        out["task"] = asyncio.run(task_pass(app))
        out["ok"] = True
    except Exception as e:
        import traceback
        out["exc"] = "%s: %s %s" % (type(e).__name__, e, traceback.format_exc()[-900:])
    child_finish(out, fh)


async def task_pass(app):
    import asyncio
    t = asyncio.ensure_future(app.align_task())
    await asyncio.sleep(0.05)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    app.align.host = None                       # a broken host: tick swallows it
    app.align.ch = 0
    app.align.last_req = app.align.clock()
    app.align.tick()
    return {"errors": app.align.errors}


def main():
    r = spawn(os.path.abspath(__file__), "head")
    check("align: Concentrator builds with the scope, head on ch1, PT level on ch1",
          r["ok"] and r.get("has_align") and r.get("heads", [None])[0] == "new"
          and r.get("pt_ch") == 0, r.get("exc") or (r.get("heads"), r.get("pt_ch")))
    check("align: idle scope records nothing", r.get("idle_samples") == 0, r.get("idle_samples"))
    info = r.get("info") or {}
    check("align: info has the 8 kinds, ch1 active, the PT wire",
          len(info.get("kinds") or []) == 8 and info.get("active") == [1]
          and (info.get("pt") or {}).get("gp") == 27, info)
    check("align: scope starts on the head", (r.get("scope") or {}).get("ok") is True, r.get("scope"))
    b = r.get("body") or {}
    check("align: frames flow to the asking client only, never broadcast",
          r.get("n_frames", 0) >= 3 and r.get("to_one_client") and not r.get("broadcast_align"),
          (r.get("n_frames"), r.get("broadcast_align")))
    check("align: every field align.js reads, plus pt",
          {"ch", "n", "dt", "gap", "bits", "ma", "rb", "thr", "drv", "tower", "busy",
           "pt", "pt_span"} <= set(b), b)
    bits = r.get("bits") or []
    check("align: the trace shows the beam break and the high rest",
          0 in bits and 1 in bits and bits[0] == 1 and b.get("n") == len(bits), bits)
    check("align: the stroke path is unchanged: the stroke counts and is sent once",
          r.get("strokes") == 1 and r.get("stroke_events") == 1,
          (r.get("strokes"), r.get("stroke_events")))
    check("align: laser mA from GP26 with the survey's dark level",
          4.5 <= (b.get("ma") or 0) <= 5.5, b.get("ma"))
    check("align: PT level from GP27 in volts", b.get("pt") == round(31000 * 3.3 / 65535, 3)
          and b.get("pt_span") == 0.0, (b.get("pt"), b.get("pt_span")))
    d, (ts, ls) = r.get("default"), r.get("steps") or (None, None)
    check("align: threshold step -> refresher target (tune path)",
          (r.get("thr") or {}).get("ok") is True and r.get("target") == {"0": d + ts},
          (r.get("thr"), r.get("target")))
    check("align: drive step reaches the laser PWM and survives a re-survey",
          (r.get("drive") or {}).get("ok") is True and r.get("laser8_after") == 65535 - ls
          and r.get("laser8_resurvey") == 65535 - ls,
          (r.get("drive"), r.get("laser8_after"), r.get("laser8_resurvey")))
    check("align: refused while strokes are in progress, laser untouched",
          (r.get("refused") or {}).get("ok") is False
          and "tower not quiet" in (r.get("refused") or {}).get("why", "")
          and r.get("laser8_refused") == 65535 - ls, (r.get("refused"), r.get("laser8_refused")))
    check("align: ch2 (empty socket) cannot be scoped", (r.get("ch2") or {}).get("ok") is False,
          r.get("ch2"))
    tg, tg2 = r.get("tune_get") or {}, r.get("tune_get2") or {}
    check("tune_get: pt on ch1 (volts), null on ch2",
          tg.get("pt") == round(31000 * 3.3 / 65535, 3) and tg2.get("pt") is None
          and "pt" in tg2, (tg.get("pt"), tg2))
    check("align: settings.json is never written", r.get("settings_same") is True)
    check("align: /status parts unchanged",
          r.get("parts") == ["gate", "link", "mux", "refresh", "ring", "survey",
                             "watchdog"], r.get("parts"))
    check("align: the task runs, and a broken host is swallowed (counted)",
          (r.get("task") or {}).get("errors", 0) >= 1, r.get("task"))
    check("align: no 'GPxx in use'; GP27 one AnalogIn", not r.get("errors")
          and r.get("gp27_kinds") == ["AnalogIn"], (r.get("errors"), r.get("gp27_kinds")))
    finish()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
