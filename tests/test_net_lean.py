# test_net_lean.py -- link.ring_send "off" imports no Wi-Fi module.
# On "off" concentrator takes net_none as farm_ws: farm_ws, its web server
# library, link_health, ws_bounds, status_doc, sand_status/report, wifi and
# socketpool are never imported (the Pico's RAM: heads-serial hit
# MemoryError at import with ~14 kB free).  "live" and "udp" import as before.
# code.py imports the program and calls run() (no source string kept).
import ast
import json
import os
import runpy
import shutil
import sys
import tempfile
from hw import spawn, child_setup, child_finish, check, finish, NEXT_SRC

WIFI_MODULES = ("farm_ws", "adafruit_httpserver", "adafruit_connection_manager",
                "link_health", "ws_bounds", "status_doc", "sand_status", "report",
                "wifi", "socketpool")


FAKE_UID = bytes(range(8))       # the fake cpu has no uid; the Pico's is 8 bytes
FAKE_UID_HEX = "0001020304050607"


def _fake_uid():
    import microcontroller
    microcontroller.cpu.uid = FAKE_UID


def _link(mode):
    return {"link": {"ring_send": mode}}


def child(scenario, src, extra):
    fh = child_setup(src, extra)
    _fake_uid()
    out = {"ok": True}
    import concentrator
    out["farm_ws"] = concentrator.farm_ws.__name__
    out["net_off"] = concentrator.NET_OFF
    out["at_import"] = sorted(m for m in WIFI_MODULES if m in sys.modules)
    if scenario == "off":
        app = concentrator.start_app()
        out["sntp"] = app.sntp is None
        out["link_health"] = app.link_health is None
        doc = app.heartbeat_doc(count=False)
        out["hb_uid"] = doc["body"]["uid"]
        out["hb_net"] = sorted(doc["body"]["net"])
        out["hb_sync"] = doc["body"]["sync"]
        app.link_step(0.0, {"lost_at": None, "tried_at": None})   # returns at once
        out["send"] = concentrator.send({"type": "stroke", "body": {"ch": 1}})
        out["ringing"] = concentrator._net_ringing()
        app.close()
    else:
        app = concentrator.Concentrator(concentrator.cfg)
        out["link_health"] = type(app.link_health).__name__
        app.close()
    out["after"] = sorted(m for m in WIFI_MODULES if m in sys.modules)
    child_finish(out, fh)


def child_slim(scenario, src, extra):
    """serial_status and tune (the USB data port) need no Wi-Fi module."""
    fh = child_setup(src, extra)
    import serial_status  # noqa: F401
    import tune  # noqa: F401
    import hb_slim
    import ring_mode
    out = {"ok": True, "loaded": sorted(m for m in WIFI_MODULES if m in sys.modules)}
    import link_health
    out["same"] = (link_health.ring_settings is ring_mode.ring_settings
                   and link_health.udp_heartbeat_doc is hb_slim.udp_heartbeat_doc
                   and link_health.dumps_compact is hb_slim.dumps_compact
                   and link_health.HB_UDP_NET == hb_slim.HB_UDP_NET
                   and link_health.RING_QUIET_S == ring_mode.RING_QUIET_S
                   and link_health.MIN_SLICE_S == ring_mode.MIN_SLICE_S
                   and link_health.MS_PER_S == ring_mode.MS_PER_S
                   and link_health.RING_SEND == ring_mode.RING_SEND_LIVE)
    out["modes"] = [link_health.ring_settings({"link": {"ring_send": m}})[0]
                    for m in ("off", "udp", "live", "bogus")]
    out["net_plan_off"] = link_health.NET_PLAN[(link_health.N_QUIET, "off")]
    child_finish(out, fh)


def child_net_none(scenario, src, extra):
    fh = child_setup(src, extra)
    _fake_uid()
    import net_none
    import ring_mode
    import hb_slim
    out = {"ok": True}
    out["quiet_default"] = net_none._ring["quiet_s"] == ring_mode.RING_QUIET_S
    out["cfg"] = net_none.configure_ring({"link": {"ring_send": "off", "ring_quiet_s": 30}})[0]
    out["quiet_cfg"] = net_none._ring["quiet_s"]
    out["idle"] = net_none.net_ringing(100.0)
    out["hb_sent"] = net_none.send({"type": "heartbeat"}, keep=False)
    out["after_hb"] = net_none._ring["last_stroke"] is None
    out["stroke_sent"] = net_none.send({"type": "stroke"})
    t = net_none._ring["last_stroke"]
    out["ringing"] = net_none.net_ringing(t + 29.0)
    out["quiet"] = net_none.net_ringing(t + 30.0)
    out["brief"] = sorted(net_none.link_brief(hb_slim.HB_UDP_NET))
    out["mode"] = [net_none.net_off(), net_none.net_udp(), net_none.net_rejoins(),
                   net_none.wifi_ok(), net_none.ws_clients(), net_none.undelivered_strokes()]
    out["uid"] = net_none._device_uid()
    out["no_ws_send"] = not hasattr(net_none, "send_to")   # sandheads: serial only
    child_finish(out, fh)


def used_farm_ws_names():
    """Every farm_ws.<name> concentrator.py reads directly (not via getattr)."""
    tree = ast.parse(open(os.path.join(NEXT_SRC, "concentrator.py")).read())
    return sorted({n.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                   and n.value.id == "farm_ws"})


def run_code_py(program_src):
    """code.py in a temp dir with a startup program; -> (globals, calls)."""
    d = tempfile.mkdtemp(prefix="codepy_")
    try:
        json.dump({"startup": {"program": "prog.py"}}, open(os.path.join(d, "settings.json"), "w"))
        open(os.path.join(d, "prog.py"), "w").write(program_src)
        cwd, path = os.getcwd(), list(sys.path)
        import time
        sleep = time.sleep
        time.sleep = lambda s: None
        os.chdir(d)
        sys.path.insert(0, d)
        sys.modules.pop("prog", None)
        try:
            g = runpy.run_path(os.path.join(NEXT_SRC, "code.py"), run_name="__main__")
            prog = sys.modules.get("prog")
            return g, getattr(prog, "CALLS", None)
        finally:
            time.sleep = sleep
            os.chdir(cwd)
            sys.path[:] = path
            sys.modules.pop("prog", None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        scen = sys.argv[2]
        fn = {"slim": child_slim, "net_none": child_net_none}.get(scen, child)
        fn(*sys.argv[2:5])
    else:
        r = spawn(os.path.abspath(__file__), "off", patch=_link("off"))
        check("off: farm_ws is net_none", r.get("farm_ws") == "net_none" and r.get("net_off"), r)
        check("off: no Wi-Fi module at import", r.get("at_import") == [], r.get("at_import") or r.get("exc"))
        check("off: none after start_app, a heartbeat, link_step and a stroke",
              r.get("after") == [], r.get("after"))
        check("off: no SntpClock, no LinkHealth", r.get("sntp") and r.get("link_health"), r)
        check("off: heartbeat still has uid, slim net and sync",
              r.get("hb_uid") == FAKE_UID_HEX and r.get("hb_net") == ["drops", "last_drop_reason"]
              and r.get("hb_sync") == "unsynced", r)
        check("off: a stroke is not sent and marks ringing",
              r.get("send") is False and r.get("ringing") is True, r)

        for mode in ("live", "udp"):
            r = spawn(os.path.abspath(__file__), mode, patch=_link(mode))
            check("%s: farm_ws and link_health as before" % mode,
                  r.get("farm_ws") == "farm_ws" and r.get("net_off") is False
                  and "link_health" in (r.get("at_import") or [])
                  and r.get("link_health") == "LinkHealth", r)
        r = spawn(os.path.abspath(__file__), "live")              # no link section at all
        check("no link section: live (as before)", r.get("farm_ws") == "farm_ws", r)

        r = spawn(os.path.abspath(__file__), "slim")
        check("serial_status + tune import no Wi-Fi module", r.get("loaded") == [], r)
        check("link_health re-exports ring_mode / hb_slim (same objects, same values)",
              r.get("same"), r)
        check("ring_settings modes unchanged", r.get("modes") == ["off", "udp", "live", "live"], r)
        check("NET_PLAN off row unchanged", r.get("net_plan_off") == [False] * 4, r)

        r = spawn(os.path.abspath(__file__), "net_none")
        check("net_none: quiet_s default and from settings",
              r.get("quiet_default") and r.get("cfg") == "off" and r.get("quiet_cfg") == 30.0, r)
        check("net_none: nothing sent; a heartbeat is not a stroke",
              r.get("hb_sent") is False and r.get("after_hb") and r.get("stroke_sent") is False, r)
        check("net_none: ringing for ring_quiet_s after a stroke",
              r.get("idle") is False and r.get("ringing") is True and r.get("quiet") is False, r)
        check("net_none: off, no Wi-Fi, no clients, nothing kept",
              r.get("mode") == [True, False, False, False, 0, []], r)
        check("net_none: heartbeat net keys and uid",
              r.get("brief") == ["drops", "last_drop_reason"] and r.get("uid") == FAKE_UID_HEX, r)
        check("net_none: no /ws send_to (sandheads over serial only)", r.get("no_ws_send"), r)

        sys.path.insert(0, NEXT_SRC)
        import net_none
        missing = [n for n in used_farm_ws_names() if not hasattr(net_none, n)]
        check("net_none has every farm_ws name concentrator uses (%d)" % len(used_farm_ws_names()),
              not missing, missing)

        g, calls = run_code_py("CALLS = []\ndef run():\n    CALLS.append('run')\n")
        check("code.py imports the program and calls run() once", calls == ["run"], calls)
        check("code.py keeps no source string or settings copy",
              "code" not in g and "config" not in g, sorted(k for k in g if not k.startswith("__")))
        g, calls = run_code_py("CALLS = ['import']\n")
        check("code.py: a program without run() runs at import", calls == ["import"], calls)
        finish()
