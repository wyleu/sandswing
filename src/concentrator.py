"""
concentrator.py
===============
Boot program for the concentrator Pico.
code.py stays the launcher and should name this file in
settings.json -> startup.program. This file does not replace the launcher.

It owns the tower and is the root: it builds every object and hands each
one what it needs. Leaf modules never import each other's internals.

Flow
----
1. Startup: PWM slice check (tower_build), sense/laser/mux setup,
   early panel indication, loopback self-test, time sync (sntp_pico,
   if Wi-Fi and time.ntp_ips).
2. Survey lifecycle is owned by SurveyState (idle / examining /
   surveying / settled). concentrator only triggers it (boot, plug-change
   edge, or explicit) and awaits step() from the sense task.
   survey_all / measure / classify yield so the panel can update during
   assessment.
3. Panel LEDs are owned by panel_task. It reads the current phase and
   kinds and flashes from panel.LED_KIND rates. LED6 shows the colour
   tower_watch decides (via StatusSlot). The same frame goes to the strip
   and to the ws payload.
4. Ring: each time the survey settles, _sense_step (re)starts ring.Ring
   with the surveyed kinds; it holds the heads' lasers and sends MIDI.
   The watchdog (watchdog_guard, OFF by default) is armed only after
   startup, just before the steady loop, and fed only from _sense_step().
5. Steady loop (sense_task): poll sense, tower, gate, await survey step,
   feed watchdog. panel_task and the ws poll task run alongside.

The old test-program exec (settings test.program) is gone; see RETIRE.md.
On tower-inactive the absolute head state can be re-assessed by triggering
SurveyState; ringing mode does not survey.

settings.json
-------------
  "startup": { "program": "concentrator.py" }

Pins
----
The pin list comes from pins_from_settings.load_pinmap. A/B/C, GP26,
GP21 and GP28 are claimed once by tower_build/mux_select; sense inputs
and lasers once here, for the whole run.
"""

import time

import asyncio
import board
import digitalio
import neopixel
import wifi
import os

try:
    import farm_log
except ImportError:
    farm_log = None


from config_loader import load_config
from tower_watch import (
    TowerWatch,
    RINGING,
    STOOD,
    IDLE,
)
from pins_from_settings import load_pinmap
from farm_ws import connect_wifi, start_server, send
import farm_ws
from tower_build import build_from_pins
from connector_boards import DEFAULT_BOARDS
from socket_survey import SocketSurvey, survey_all
from channel_gate import ChannelGate
from threshold_refresh import refresher_from_cfg
from ring import Ring, open_midi, FULL, MIN_STROKE_GAP_MS
import panel
import report
from board_pins import release
from watchdog_guard import WatchdogGuard
from sand_event import Emitter, make_event
from sntp_pico import TickClock
from survey_state import SurveyState, SURVEYING, SETTLED, IDLE as SURVEY_IDLE
from mux_errors import ChannelBusy
from laser_measure import counts_to_ma
from adc_avg import avg as _avg

RING_WINDOW_S = 0.4
SETTLE_S = 0.3
# survey kind -> the head names this file and sand_status already use
HEAD_NAME = {"head_new": "new", "head_old": "old", "loopback": "lb",
             "trigger": "trigger",
             "empty": "empty", "no_board": "empty", "fault": "fault",
             "unknown": "unknown"}

HEARTBEAT_LENGTH = 1.25

# Link health (docs/link-health.md).  Code constants, not settings.
PANEL_SCHEMA = "sand.panel/v1"   # heartbeat frames: Sandbells shows, never stores
PICO_HEARTBEAT_S = 5.0           # heartbeat (LED mimic + link + sync) to /ws
LINK_CHECK_S = 2.0               # Wi-Fi supervision pass
WIFI_REJOIN_S = 30.0             # rejoin attempts while the tower is quiet
WIFI_REJOIN_RINGING_S = 120.0    # ...and while ringing (a join blocks sensing)
WIFI_JOIN_TIMEOUT_S = 10         # per network in the list
LINK_DEAD_S = 120.0              # Wi-Fi "up" but no client heard: radio reset
LINK_DEAD_RESET_S = 600.0        # still nothing: reset the Pico (watchdog armed only)

# Trigger plug (test switch, docs/trigger-plug.md).  Code constants.
# Stroke on the first low sample after the switch has been seen open for
# TRIGGER_REARM_MS (release bounce cannot re-fire; press bounce is ignored
# because the channel is disarmed until a clean release).
TRIGGER_REARM_MS = 50            # open this long before the next press counts
TRIGGER_MIN_GAP_MS = 150         # and never two test strokes closer than this
TRIGGER_STUCK_MS = 10000         # held this long (tower quiet): resurvey once
TRIGGER_BODY = {"test": True, "src": "trigger"}

# Survey "why" (docs/survey-why.md).  Code constants.
WHY_HB_CHARS = 80            # heartbeat: first why line, cut to this length
WHY_HB_KINDS = ("fault", "trigger", "unknown")   # ...and only for these kinds
WHY_LINES_MAX = 3            # /status + events: at most this many why lines

# empty-socket check (_watch_empty): tower inactive + survey idle only
EMPTY_CHECK_S = 1.0          # gap between checks
EMPTY_SETTLE_S = 0.02        # laser on -> read the sense ADC
EMPTY_HEAD_MA = 5.0          # laser current that means a head is now fitted

cfg = load_config()
cfg.banner()

def short_why(w, n=WHY_HB_CHARS):
    """Cut a why line to n chars, keeping its trailing "(measurements)"."""
    if len(w) <= n:
        return w
    k = w.rfind(" (")
    tail = w[k:] if k > 0 and w.endswith(")") and len(w) - k < n // 2 else ""
    return w[:n - len(tail) - 3].rstrip() + "..." + tail


def _claim_sense(sense_gps):
    pins = []
    for gp in sense_gps:
        pin = digitalio.DigitalInOut(getattr(board, "GP%d" % gp))
        pin.direction = digitalio.Direction.INPUT
        pin.pull = digitalio.Pull.UP
        pins.append(pin)
    return pins


WIFI_ROUNDS = 6          # passes over wifi.networks at boot before giving up
WIFI_RETRY_S = 10        # pause between passes (hotspot may still be starting)


def setup_net():
    for rnd in range(WIFI_ROUNDS):
        if connect_wifi(cfg):
            break
        if rnd < WIFI_ROUNDS - 1:
            emit("WiFi pass %d/%d failed – retry in %d s" % (rnd + 1, WIFI_ROUNDS, WIFI_RETRY_S))
            time.sleep(WIFI_RETRY_S)
    else:
        emit("No WiFi – lineup still runs")
        return None
    import wifi
    for _ in range(20):
        ip = wifi.radio.ipv4_address
        if ip is not None:
            break
        time.sleep(0.25)
    print("IP", ip)
    if ip is None:
        emit("No IP – server not started")
        return None
    return ip


def setup_sntp(ip):
    """SntpClock on the GPS servers by IP (spec T4), or None."""
    t = cfg.get("time", {}) or {}
    from time_servers import candidates
    ips = candidates(t.get("ntp_ips"), ip, wifi.radio.ipv4_subnet, log=emit)
    if ip is None or not ips:
        emit("No time sync (no IP or no time.ntp_ips)")
        return None
    import socketpool
    from sntp_pico import SntpClock
    opts = dict((k, t[k]) for k in ("interval_s", "burst", "max_slew_ppm",
                                    "step_ms", "ring_sample", "timeout_s",
                                    "stale_after") if k in t)
    return SntpClock(socketpool.SocketPool(wifi.radio), ips, **opts)



if farm_log:
    farm_log.start(cfg)

def emit(msg):
    print(msg)
    if farm_log:   
        farm_log.write(msg)
        
async def ws_poll_task(guard=None, app=None):
    while True:
        farm_ws.poll()
        if app is not None:
            app.flush_link_notes()
        if guard is not None:
            guard.checkin("ws_poll")       # a hung ws task stops the feeding
        await asyncio.sleep(0.05)

class Concentrator:
    def __init__(self, cfg):
        self.cfg = cfg
        # first: record why we booted (reset_reason/run_reason); never armed here
        self.guard = WatchdogGuard(cfg.get("watchdog", {}) or {})
        emit("boot reset=%s run=%s" % (self.guard.last_reset, self.guard.run_reason))
        self.pinmap = load_pinmap(cfg)
        self.sense_gps = self.pinmap["sense_gp"]
        self.n = len(self.sense_gps)          # set n first
        self.link = "no_wifi"
        self.wifi_up = False             # set by start_app; link_task follows it
        self.wifi_lost = 0               # times the Wi-Fi link was lost
        self.rejoins = 0                 # successful rejoins after a loss
        self.radio_resets = 0
        self._dead_handled = False       # one radio reset per silent spell
        self.boot_at = time.monotonic()
        self.hb_seq = 0                  # heartbeat counter (not the event seq)
        self._frame_args = None
        self.sntp = None
        
        self._empty_check = 0        # last _watch_empty run (monotonic s)
            
        self.ticks = TickClock()
        self.events = Emitter(src="sandswing",
                              device_id=cfg.get("device", {}).get("name", "concentrator"),
                              boot_id=os.urandom(4).hex(),
                              ticks=self.ticks)

                                        # set by main() if time sync is up
        self._setup_sense()          # now claim the sense pins for the rest of the run
        self._setup_strip()
        
        self._setup_tower()
        self._setup_mux()            # one A/B/C owner; PWM slice check first
        self._setup_lasers()
        self.gate = ChannelGate(self.n)
        self.survey = SocketSurvey(self.mux)

        self.survey_state = SurveyState(
            n_channels=self.n,
            survey_fn=lambda: survey_all(self.survey, self.bells, self.gate, self._boards()),
            paint_fn=lambda kinds, beat: self._paint_kinds(kinds, beat),
        )
        self.survey_state.trigger("boot")
        
        self.refresher = refresher_from_cfg(self.mux, self.bells,
                                            self.pinmap["raw"].get("threshold_mux") or {},
                                            gate=self.gate)
        self.ring = Ring(self.inputs, self.lasers, midi=open_midi(),
                         clock_ms=self.ticks.now)
        
        # early boot indication — before the long self-test
        self.kinds = {i: "examining" for i in range(min(4, self.n))}
        self._paint_kinds(self.kinds, True)

        self.selftest = self.survey.run(self._populated())   # startup only
        
        print("selftest ok=%s" % self.selftest["ok"])
        
        st = self.selftest
        for ch, tests in st["channels"].items():
            for name, t in tests.items():
                if not t.get("ok"):
                    print("   ch %d %s" % (ch + 1, name), t)
        if not st["dac"].get("ok"):
            print("   dac", st["dac"])
        
        self._init_edge_state()
        
        self.head = ["unknown"] * self.n
        self.kinds = {}
        self.fitted = set()
        # the ring starts when the boot survey settles (_sense_step)

    def _setup_sense(self):
        self.sense_gps = self.pinmap["sense_gp"]
        self.inputs = _claim_sense(self.sense_gps)
        self.n = len(self.sense_gps)

    def _setup_strip(self):
        # TODO(hardware): pixel_order. The old colour comments ((4,0,0)
        # "green") suggest the strip is RGB while neopixel assumes GRB.
        self.strip = neopixel.NeoPixel(
            getattr(board, "GP%d" % self.pinmap["neopixel_gp"]),
            panel.PANEL_N,
            brightness=0.3,
            auto_write=False,
            pixel_order=neopixel.RGB,
        )
        self.strip.fill((0, 0, 0))
        self.strip.show()

    def _setup_tower(self):
        self.status_slot = panel.StatusSlot()     # LED6 colour, via panel
        self.tower = TowerWatch(self.n, status_pixel=self.status_slot)

    def _setup_mux(self):
        # self.mux is the 4051 select Tower (self.tower stays tower_watch).
        self.mux, self.bells = build_from_pins(self.pinmap["raw"])
        for w in self.mux.warnings:
            emit("pins: " + w)
        drive = self.bells[0].drive if self.bells else None
        self.thr_pwm = drive.pwm if drive else None      # GP21, threshold_mux.pwm_hz

    def _setup_lasers(self):
        pins = self.mux.pins
        self.lasers = [pins.pwm_out(gp, frequency=self.pinmap["laser_hz"], duty_cycle=0)
                       for gp in self.pinmap["laser_gp"]]
        for b in self.bells:
            b.laser = self.lasers[b.index]
            b.sense_pin = self.inputs[b.index]

    def _boards(self):
        return self.pinmap["raw"].get("connector_boards") or DEFAULT_BOARDS

    def _populated(self):
        boards = self._boards()
        on = [c for bd in boards if bd.get("present") is not False for c in bd["channels"]]
        return [b for b in self.bells if b.index in on]

    async def _start_ring(self, kinds):
        """Settle, then ring the surveyed heads.
        TODO(spec C4): settle from measured delays."""
        await asyncio.sleep(SETTLE_S)
        self.ring.start(kinds)        # TODO(spec section 6): per-head laser_duty

    def _init_edge_state(self):
        self.last_false = [None] * self.n
        self.edge_at = [None] * self.n
        self.stroke_ms = [None] * self.n     # tick ms of the last accepted stroke
        self._prev_held = [not pin.value for pin in self.inputs]
        self.trig_armed = [False] * self.n   # trigger plug: next press counts
        self.trig_open_ms = [None] * self.n  # tick ms the switch was seen open
        self.trig_low_ms = [None] * self.n   # tick ms the press began
        self.trig_stuck = [False] * self.n
        self.trig_presses = [0] * self.n
        # survey result per channel: kind, why, and for a fault what latched it
        self.chan = [{"kind": "unknown", "why": [], "latched": None}
                     for _ in range(self.n)]
        self.frame = panel.panel_frame({}, self.link)
        
    def status_payload(self):
        parts = {"mux": report.part(self.mux), "gate": report.part(self.gate),
                 "refresh": report.part(self.refresher),
                 "ring": report.part(self.ring),
                 "sync": report.part(self.sntp, "boot_record"),
                 "watchdog": report.part(self.guard),
                 "link": self.link_status(),
                 "survey": self.survey_status()}
        return report.build({
            "id": self.cfg.get("device", {}).get("name"),
            "name": self.cfg.get("device", {}).get("name"),
            "family": self.cfg.get("device", {}).get("family"),
            "role": self.cfg.get("device", {}).get("role"),
            "location": self.cfg.get("device", {}).get("location"),
            "ip": str(wifi.radio.ipv4_address),
            "ssid": self.cfg.get("wifi", {}).get("ssid"),
            "heads": list(self.head),
            "tower": self.tower.tower,          # "active" | "ringing_down" | "inactive"
            "ts": time.time(),                 # TODO(spec T13): utc_ms from sntp
        }, parts, panel.payload(self.frame))


    def gps_day(self):
        """Call when the GPS clock head announces the day boundary."""
        self.tower.gps_tick()

    def poll_sense(self):
        """Active-low sense, pull-up. A falling edge opens a ring window.
        A return held with no edge is STOOD, not an edge.
        """
        now = time.monotonic()
        for i, pin in enumerate(self.inputs):
            self._channel(i, pin, now)
        self.ring.poll()

    def _paint_kinds(self, kinds, beat_on):
        self.kinds = kinds
        self.head = [HEAD_NAME.get(kinds.get(i, "unknown"), "unknown")
                     for i in range(self.n)]
        self._paint(beat_on)


    def _paint(self, beat_on):
        tripped = self.ring.tripped()
        kinds = dict((c, "response" if (c in tripped or self._trigger_down(c))
                      else self.kinds.get(c, "unknown"))
                     for c in range(4))
        
        
        activity = {"active": "ringing", "ringing_down": "ringing_down"}.get(
            self.tower.tower, "heartbeat")
        
        if activity == "heartbeat":
            st = self.sntp.state() if self.sntp is not None else "unsynced"
            if st == "stale":
                activity = "heartbeat_holdover"   # amber: still stamping utc
            elif st != "synced":
                activity = "heartbeat_nosync"     # red: no utc at all
        
        right = None if self.tower.tower == "inactive" else self.status_slot.colour
        
        if activity == "heartbeat" and self.guard.panel_state():
            # last boot was a watchdog reset: LED6 blinks magenta with the
            # heartbeat until acknowledged or a clean day (watchdog.ack_after_s)
            activity, right = "watchdog_reset", (None if beat_on else (0, 0, 0))
        self.frame = panel.panel_frame(kinds, self.link, activity, beat_on,
                                       right=right)
        self._frame_args = (kinds, self.link, activity, right)
        panel.paint(self.strip, self.frame)
        
    def _channel(self, i, pin, now):
        prev = self._prev_held[i]
        held = not pin.value
        became = held and not prev
        released = (not held) and prev
        self._prev_held[i] = held

        # plug-change → ask SurveyState (only on a real edge, only for non-head channels)
        if (became or released) and self.head[i] in ("lb", "empty", "fault"):
            self.survey_state.trigger("plug", channel=i)
            print("%.3f ch %d plug change" % (time.monotonic(), i + 1))
            self.tower.report(i, IDLE, False)
            return IDLE, held
    
        stroke = False
        if self.head[i] == "trigger":
            stroke = self._trigger_edge(i, held, now)
        elif became and self.last_false[i] is not None:
            self.edge_at[i] = now
            t_ms, last = self.ticks.now(), self.stroke_ms[i]
            if last is not None and t_ms - last < MIN_STROKE_GAP_MS:
                print("%.3f ch %d DOUBLE %dms (ignored)" % (now, i + 1, t_ms - last))
            else:
                self.stroke_ms[i] = t_ms
                stroke = True
                print("%.3f ch %d EDGE" % (now, i + 1))

        if not held:
            self.last_false[i] = now
            if self.tower.test_mark[i]:
                self.tower.clear_test(i)
        if self.edge_at[i] is not None and (now - self.edge_at[i]) < RING_WINDOW_S:
            kind = RINGING
        elif held:
            kind = STOOD
        else:
            kind = IDLE
        if became:
            print("%.3f ch %d HELD kind=%s" % (now, i + 1, kind))
        elif released:
            print("%.3f ch %d CLEAR" % (now, i + 1))
        if held and i not in self.fitted:
            self.fitted.add(i)
            self.tower.set_fitted(i, True)
            print("%.3f ch %d fitted" % (now, i + 1))
        self.tower.report(i, kind, held)
        if stroke:
            # the tower (and so the time session) wakes on this stroke:
            # switch to ringing first so the stroke carries the new session
            self._sync_ringing()
            body = {"ch": i + 1}
            if self.head[i] == "trigger":
                body.update(TRIGGER_BODY)        # labelled, never hidden
            send(self.events.make(self.sntp, "stroke", body))
        return kind, held

    # ---- survey why ---------------------------------------------------------
    def _survey_settled(self, results, kinds):
        """One console line per channel, and a stored "channel" event for
        each channel whose kind changed (prev_kind, why)."""
        results = results or {}
        for i in range(self.n):
            r = results.get(i) or {}
            kind = kinds.get(i, r.get("kind", "unknown"))
            why = [str(w) for w in (r.get("why") or [])][:WHY_LINES_MAX]
            c = self.chan[i]
            prev = c["kind"]
            changed = kind != prev
            emit("ch %d %s: %s%s" % (i + 1, kind, "; ".join(why) or "-",
                                     (" (was %s)" % prev) if changed else ""))
            c["kind"], c["why"] = kind, why
            if not changed:
                continue
            ev = {}
            try:                                 # never let a report stop the ring
                ev = self.events.make(self.sntp, "channel",
                                      {"ch": i + 1, "kind": kind,
                                       "prev_kind": prev, "why": why})
                send(ev)
            except Exception as e:
                emit("channel event ch %d: %r" % (i + 1, e))
            if kind == "fault":
                c["latched"] = {"why": why, "seq": ev.get("seq"),
                                "uptime_s": int(time.monotonic() - self.boot_at),
                                "utc_ms": ev.get("utc_ms")}
            else:
                c["latched"] = None

    def survey_status(self):
        """/status parts.survey: kind + why per channel, and for a fault the
        why, event seq and time that latched it."""
        return {"phase": self.survey_state.phase,
                "channels": [dict(ch=i + 1, **c) for i, c in enumerate(self.chan)]}

    def chan_brief(self):
        """Heartbeat: [kind, short why or None] per channel (why only for
        fault / trigger / unknown, first line, WHY_HB_CHARS max)."""
        out = []
        for c in self.chan:
            w = None
            if c["kind"] in WHY_HB_KINDS and c["why"]:
                w = short_why(c["why"][0])
            out.append([c["kind"], w])
        return out

    def _trigger_down(self, i):
        prev = getattr(self, "_prev_held", None)     # not yet at early boot
        return (prev is not None and i < self.n
                and self.head[i] == "trigger" and prev[i])

    def _trigger_edge(self, i, held, now):
        """Trigger plug: True when this sample is a counted press."""
        t = self.ticks.now()
        opened = self.trig_open_ms[i]
        if opened is not None and t < opened:
            opened = self.trig_open_ms[i] = t        # clock replaced: start again
        if not held:
            self.trig_low_ms[i] = None
            self.trig_stuck[i] = False
            if opened is None:
                self.trig_open_ms[i] = t
            elif t - opened >= TRIGGER_REARM_MS:
                self.trig_armed[i] = True
            return False
        if opened is not None and t - opened >= TRIGGER_REARM_MS:
            self.trig_armed[i] = True                # open long enough before this press
        self.trig_open_ms[i] = None
        if self.trig_low_ms[i] is None:
            self.trig_low_ms[i] = t
        elif (not self.trig_stuck[i] and self.tower.tower == "inactive"
              and t - self.trig_low_ms[i] >= TRIGGER_STUCK_MS):
            # a hand does not hold a push switch this long: a head swapped
            # in, or a stuck switch.  Look again (once per hold).
            self.trig_stuck[i] = True
            print("%.3f ch %d trigger held %d ms: resurvey" % (now, i + 1, t - self.trig_low_ms[i]))
            self.survey_state.trigger("trigger-held", channel=i)
        if not self.trig_armed[i]:
            return False
        self.trig_armed[i] = False
        last = self.stroke_ms[i]
        if last is not None and t - last < TRIGGER_MIN_GAP_MS:
            print("%.3f ch %d TRIGGER %dms after the last (ignored)" % (now, i + 1, t - last))
            return False
        self.stroke_ms[i] = t
        self.edge_at[i] = now
        self.trig_presses[i] += 1
        print("%.3f ch %d TRIGGER press %d" % (now, i + 1, self.trig_presses[i]))
        return True


    def _sync_ringing(self):
        """Time session follows the tower: active / ringing_down = ringing."""
        ringing = self.tower.tower in ("active", "ringing_down")
        if self.sntp is not None:
            self.sntp.set_ringing(ringing)

    def _tower_step(self):
        self._sync_ringing()
        self.gate.enforce(self.bells)
        
    async def _watch_empty(self, now):
        """Look for a head plugged into an 'empty' socket: pulse its laser
        and read its current.  Only while the tower is inactive and the
        survey idle (the pulse can trip a neighbour's near-threshold head).
        Awaited from _sense_step, so gate.enforce cannot cut the pulse."""
        if self.tower.tower != "inactive" or self.survey_state.phase != SURVEY_IDLE:
            return
        if now - self._empty_check < EMPTY_CHECK_S:
            return
        self._empty_check = now
        checked = False
        for i, kind in enumerate(self.head):
            if kind != "empty":
                continue
            ma = await self._laser_current_ma(self.bells[i])
            if ma is None:
                continue                         # mux busy: next time
            checked = True
            if ma > EMPTY_HEAD_MA:
                self.survey_state.trigger("empty-head", channel=i)
                break
        if checked:
            # the select moved the output 4051: refresh the hold caps now
            for _ in self.bells:
                self.refresher.step()

    async def _laser_current_ma(self, bell):
        """Laser current in mA, as di_ma in socket_survey.measure; None if
        the mux is held (same ChannelBusy rule the refresher uses)."""
        adc = bell.sense.adc
        try:
            with self.mux.select(bell):
                bell.laser.duty_cycle = 0
                dark = _avg(adc, self.survey.n_avg)
                bell.laser.duty_cycle = FULL
                await asyncio.sleep(EMPTY_SETTLE_S)
                on = _avg(adc, self.survey.n_avg)
        except ChannelBusy:
            return None
        finally:
            bell.laser.duty_cycle = 0
        return counts_to_ma(on - dark)

    def close(self):
        """Release everything this file and tower_build claimed."""
        import mux_select
        self.ring.stop()
        release(self.lasers)
        release(self.inputs)
        if self.thr_pwm is not None:
            release([self.thr_pwm])
        mux_select.reset_registry()
        self.strip.fill((0, 0, 0))
        self.strip.show()
        release([self.strip])
     

    async def _sense_step(self):       
        self.poll_sense()
        self.tower.poll()
        self._tower_step()
        prev = self.survey_state.phase
        phase = await self.survey_state.step(True)
        if phase != prev and phase == SURVEYING:
            self.ring.stop()          # lasers are the survey's until it settles
        elif phase != prev and phase == SETTLED:
            # update the gate here (not at SURVEYING): clearing early left
            # parts.gate.kind as 'unknown' for the whole survey, and a
            # corrected plug needs clear() before set_kind can leave a fault
            for i in range(self.n):
                self.gate.clear(i)
            for i, kind in self.survey_state.kinds.items():
                self.gate.set_kind(i, kind)   # else gate.enforce zeroes the heads
            # head[] now, not at the next panel pass: _watch_empty reads it
            self._paint_kinds(self.survey_state.kinds, True)
            self._survey_settled(self.survey_state.last_result,
                                 self.survey_state.kinds)
            await self._start_ring(self.survey_state.kinds)
        await self._watch_empty(time.monotonic())
        self.guard.loop_tick()

    async def sense_task(self):  # or loop()
        while True:
            await self._sense_step()
            await asyncio.sleep(0.02)

    async def panel_task(self):
        import panel as P
        from survey_state import PHASE_KIND
        while True:
            phase = self.survey_state.phase
            override = PHASE_KIND.get(phase)
            ch = getattr(self.survey_state, "_pending_ch", None)
            if override:
                if ch is not None:
                    kinds = dict(self.kinds)
                    kinds[ch] = override
                else:
                    kinds = {i: override for i in range(4)}
            else:
                kinds = self.survey_state.kinds or self.kinds

            # channel flash from LED_KIND rate
            rate = 0
            if override and override in P.LED_KIND:
                rate = P.LED_KIND[override][1]
            channel_on = True if rate <= 0 else ((time.monotonic() * (rate + 1)) % 1.0) < 0.5

            # LED1: green while Sandbells is connected and heard (a client
            # that sends heartbeats); link_task sets "no_wifi" on a loss
            if self.link != "no_wifi":
                self.link = ("ws_client" if farm_ws.sandbells_connected()
                             else "wifi_only")

            # heartbeat beat (always toggles)
            heartbeat_on = (time.monotonic() % HEARTBEAT_LENGTH) < (HEARTBEAT_LENGTH /2) 

            # paint channels with channel_on; heartbeat uses heartbeat_on
            # simplest: call _paint with heartbeat_on so LED6 flashes,
            # and let panel_frame blank channels only when their own rate says so
            self._paint_kinds(kinds, heartbeat_on if rate == 0 else channel_on)
            await asyncio.sleep(0.05)
        
    # ---- link health ------------------------------------------------------
    def link_event(self, what, **info):
        """A "link" sand.event/v1 (stored by Sandbells, kept for replay)."""
        body = {"what": what}
        body.update(info)
        emit("%.3f link %s %s" % (time.monotonic(), what, info))
        send(self.events.make(self.sntp, "link", body))

    def flush_link_notes(self):
        take = getattr(farm_ws, "take_link_notes", None)
        if take is None:
            return
        for what, info in take():
            self.link_event(what, **info)

    def link_status(self):
        st = getattr(farm_ws, "link_status", None)
        d = st() if st is not None else {}
        d.update({"wifi_up": self.wifi_up, "wifi_lost": self.wifi_lost,
                  "rejoins": self.rejoins, "radio_resets": self.radio_resets})
        return d

    def _mimic(self):
        """The panel as it looks in its ON phase (a heartbeat sampled in
        the dark half of a blink would read as black)."""
        if self._frame_args is None:
            return panel.payload(self.frame)
        kinds, link, activity, right = self._frame_args
        return panel.payload(panel.panel_frame(kinds, link, activity, True,
                                               right=right))

    def heartbeat_doc(self):
        """sand.panel/v1 heartbeat: same envelope as events (type, src, id,
        boot_id, seq = heartbeat count, stamp fields, body)."""
        self.hb_seq += 1
        sn = self.sntp
        body = self._mimic()
        body.update({
            "link": self.link,
            "tower": self.tower.tower,
            "net": self.link_status(),
            "wifi": {"ip": farm_ws.wifi_ip() if hasattr(farm_ws, "wifi_ip") else None,
                     "rssi": farm_ws.wifi_rssi() if hasattr(farm_ws, "wifi_rssi") else None},
            "sync": sn.state() if sn is not None else "unsynced",
            "time": None if sn is None else {
                "srv": sn.server, "stratum": sn.stratum, "last": sn.last,
                "peer": getattr(sn, "peer", None)},
            "last_seq": self.events.seq,          # newest event seq this boot
            "chan": self.chan_brief(),
            "uptime_s": int(time.monotonic() - self.boot_at),
            "last_reset": self.guard.last_reset,
            "watchdog": self.guard.armed,
        })
        try:
            import gc
            body["mem_free"] = gc.mem_free()
        except (ImportError, AttributeError):
            pass
        doc = make_event("heartbeat", sn, body, src=self.events.src,
                         device_id=self.events.device_id,
                         boot_id=self.events.boot_id, seq=self.hb_seq,
                         ticks=self.ticks)
        doc["schema"] = PANEL_SCHEMA
        return doc

    async def heartbeat_task(self):
        while True:
            await asyncio.sleep(PICO_HEARTBEAT_S)
            if farm_ws.ws_clients():
                try:
                    send(self.heartbeat_doc(), keep=False)
                except Exception as e:           # never let it kill main()
                    emit("heartbeat: %r" % e)

    def _rejoin(self):
        """Blocking join (paused watchdog).  True if we have an address."""
        with self.guard.paused_for():
            farm_ws.connect_wifi(self.cfg, timeout=WIFI_JOIN_TIMEOUT_S)
        return farm_ws.wifi_ok()

    def _wifi_regained(self, down_s):
        ip = farm_ws.wifi_ip()
        self.wifi_up = True
        self.link = "wifi_only"
        if farm_ws.server_running() and farm_ws.server_host() != ip:
            farm_ws.stop_server()                # new address: rebind
        if not farm_ws.server_running():
            start_server(self.cfg, self.status_payload)
        if self.sntp is None and ip is not None:
            self.sntp = setup_sntp(ip)           # boot had no Wi-Fi
            if self.sntp is not None:
                asyncio.create_task(self.sntp.run())
        self.link_event("wifi_back", ip=ip, down_s=down_s, rejoins=self.rejoins)

    def link_step(self, now, state):
        """One supervision pass.  state: {"lost_at", "tried_at"}."""
        if not farm_ws.wifi_ok():
            if self.wifi_up:
                self.wifi_up = False
                self.wifi_lost += 1
                self.link = "no_wifi"
                state["lost_at"] = now
                state["tried_at"] = now          # give it a moment by itself
                farm_ws.drop_all("wifi lost")
                self.link_event("wifi_lost", count=self.wifi_lost)
            gap = (WIFI_REJOIN_S if self.tower.tower == "inactive"
                   else WIFI_REJOIN_RINGING_S)
            if now - state["tried_at"] >= gap:
                state["tried_at"] = now
                if self._rejoin():
                    self.rejoins += 1
            return
        if not self.wifi_up:
            lost = state.get("lost_at")
            self._wifi_regained(None if lost is None else round(now - lost, 1))
            return
        # Wi-Fi says up.  Is anything getting through?
        if not farm_ws.stats.get("hb_seen"):
            return                               # old Sandbells / bench: no rule
        age = farm_ws.last_rx_age_s(now)
        if age is None or age < LINK_DEAD_S:
            self._dead_handled = False
            return
        if self.tower.tower != "inactive":
            return                               # never during ringing
        if age >= LINK_DEAD_RESET_S and self.guard.armed:
            self.link_event("reset", reason="nothing heard %d s" % age)
            farm_ws.poll()                       # last chance to send it
            import microcontroller
            microcontroller.reset()
        if not self._dead_handled:
            self._dead_handled = True
            self.radio_resets += 1
            self.link_event("link_dead", reason="nothing heard %d s" % age,
                            sync=self.sntp.state() if self.sntp else None)
            farm_ws.reset_radio()                # next pass: lost -> rejoin

    async def link_task(self):
        state = {"lost_at": None, "tried_at": 0.0}
        while True:
            await asyncio.sleep(LINK_CHECK_S)
            try:
                self.link_step(time.monotonic(), state)
            except Exception as e:               # never let it kill main()
                emit("link_task: %r" % e)


def start_app():
    """Everything up to the steady loop: net, Concentrator (survey,
    classify, self-test, ring), first time sync, server.  The watchdog is
    armed last."""
    ip = setup_net()
    app = Concentrator(cfg)
    app.link = "wifi_only" if ip else "no_wifi"   # panel_task: ws_client when /ws connected
    app.wifi_up = bool(ip)                        # link_task rejoins if not

    app.sntp = setup_sntp(ip)
    
    if app.sntp is not None:
        try:
            app.sntp.burst_once()                 # first sync before arming
        except Exception as e:
            emit("first sync failed: %s" % e)
    start_server(cfg, app.status_payload)
    app.guard.arm()
    return app


async def main():
    app = start_app()
    tasks = [
        app.sense_task(),
        app.panel_task(),
        ws_poll_task(app.guard, app),
        app.refresher.run(),
        app.heartbeat_task(),
        app.link_task(),
        ]
    
    if app.sntp is not None:
        tasks.append(app.sntp.run())
    try:
        await asyncio.gather(*tasks)
    finally:
        app.guard.disarm("exit")                  # Ctrl-C: REPL is not reset under you
        app.close()
        if farm_log:
            try:
                farm_log.close()
            except Exception:
                pass
        print("Cleanup complete")

def run():
    asyncio.run(main())


if __name__ == "__main__" or __name__ == "<module>":
    run()
