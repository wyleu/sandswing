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
   feed watchdog. panel_task and status/ws tasks run alongside.

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
    status_pixel,
    RINGING,
    STOOD,
    IDLE,
)
from pins_from_settings import load_pinmap
from farm_ws import connect_wifi, start_server, poll, send
import farm_ws
from tower_build import build_from_pins
from connector_boards import DEFAULT_BOARDS
from socket_survey import SocketSurvey, survey_all
from channel_gate import ChannelGate
from threshold_refresh import refresher_from_cfg
from ring import Ring, open_midi, FULL
import panel
import report
from board_pins import release
from watchdog_guard import WatchdogGuard
from sand_event import Emitter
from sntp_pico import TickClock
from survey_state import SurveyState, SURVEYING, SETTLED, IDLE as SURVEY_IDLE
from mux_errors import ChannelBusy
from laser_measure import counts_to_ma
from adc_avg import avg as _avg

CONFIG_FILE = "settings.json"
RING_WINDOW_S = 0.4
SETTLE_S = 0.3
# survey kind -> the head names this file and sand_status already use
HEAD_NAME = {"head_new": "new", "head_old": "old", "loopback": "lb",
             "empty": "empty", "no_board": "empty", "fault": "fault",
             "unknown": "unknown"}

HEARTBEAT_LENGTH = 1.25

# empty-socket check (_watch_empty): tower inactive + survey idle only
EMPTY_CHECK_S = 1.0          # gap between checks
EMPTY_SETTLE_S = 0.02        # laser on -> read the sense ADC
EMPTY_HEAD_MA = 5.0          # laser current that means a head is now fitted

server = None

cfg = load_config()
cfg.banner()

def load_settings():
    import json
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def _claim_sense(sense_gps):
    pins = []
    for gp in sense_gps:
        pin = digitalio.DigitalInOut(getattr(board, "GP%d" % gp))
        pin.direction = digitalio.Direction.INPUT
        pin.pull = digitalio.Pull.UP
        pins.append(pin)
    return pins


def _release(pins):
    for pin in pins:
        try:
            pin.deinit()
        except Exception:
            pass

def setup_net():
    if not connect_wifi(cfg):
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
    ips = t.get("ntp_ips") or []
    if ip is None or not ips:
        emit("No time sync (no IP or no time.ntp_ips)")
        return None
    import socketpool
    from sntp_pico import SntpClock
    opts = dict((k, t[k]) for k in ("interval_s", "burst", "max_slew_ppm",
                                    "step_ms", "ring_sample") if k in t)
    return SntpClock(socketpool.SocketPool(wifi.radio), ips, **opts)



if farm_log:
    farm_log.start(cfg)

def emit(msg):
    global _last_line
    _last_line = str(msg)
    print(msg)
    if farm_log:   
        farm_log.write(msg)
        
async def ws_poll_task(guard=None):
    while True:
        farm_ws.poll()
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
        self._inactive_surveyed = True
        self._inactive_since = time.monotonic()
        
        self.refresher = refresher_from_cfg(self.mux, self.bells,
                                            self.pinmap["raw"].get("threshold_mux") or {},
                                            gate=self.gate)
        self.ring = Ring(self.inputs, self.lasers, midi=open_midi())
        
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
        self._prev_tower = self.tower.tower
        
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
        self.thr_sense = drive.back if drive else None   # GP28 monitor

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
        self._still_since = [time.monotonic()] * self.n
        self._still_held = [not pin.value for pin in self.inputs]
        self.last_false = [None] * self.n
        self.edge_at = [None] * self.n
        self._prev_held = list(self._still_held)
        self._rescan_due = None
        self.high_since = [time.monotonic()] * self.n
        self.frame = panel.panel_frame({}, self.link)
        
    def status_payload(self):
        parts = {"mux": report.part(self.mux), "gate": report.part(self.gate),
                 "refresh": report.part(self.refresher),
                 "ring": report.part(self.ring),
                 "sync": report.part(self.sntp, "boot_record"),
                 "watchdog": report.part(self.guard)}
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

    def report_head(self, index, kind, return_held):
        self.tower.report(index, kind, return_held)

    def poll_sense(self):
        """Active-low sense, pull-up. A falling edge opens a ring window.
        A return held with no edge is STOOD, not an edge.
        """
        now = time.monotonic()
        for i, pin in enumerate(self.inputs):
            kind, held = self._channel(i, pin, now)
        self.ring.poll()

        tpm = (self.cfg.get("escapement", {}) or {}).get("tpm", 48)
        period = 120.0 / tpm                 # one pulse per two clock beats

    def _paint_kinds(self, kinds, beat_on):
        self.kinds = kinds
        self.head = [HEAD_NAME.get(kinds.get(i, "unknown"), "unknown")
                     for i in range(self.n)]
        self._paint(beat_on)


    def _paint(self, beat_on):
        tripped = self.ring.tripped()
        kinds = dict((c, "response" if c in tripped else self.kinds.get(c, "unknown"))
                     for c in range(4))
        
        
        activity = {"active": "ringing", "ringing_down": "ringing_down"}.get(
            self.tower.tower, "heartbeat")
        
        if activity == "heartbeat":
            synced = self.sntp is not None and getattr(self.sntp, "synced", False)
            if not synced:
                activity = "heartbeat_nosync"
        
        right = None if self.tower.tower == "inactive" else self.status_slot.colour
        
        if activity == "heartbeat" and self.guard.panel_state():
            # last boot was a watchdog reset: LED6 blinks magenta with the
            # heartbeat until acknowledged or a clean day (watchdog.ack_after_s)
            activity, right = "watchdog_reset", (None if beat_on else (0, 0, 0))
        self.frame = panel.panel_frame(kinds, self.link, activity, beat_on,
                                       right=right)
        panel.paint(self.strip, self.frame)
        
    def _settle(self, i, held, now):
        if held != self._still_held[i]:
            self._still_held[i] = held
            self._still_since[i] = now
            return
        if i not in self.fitted:
            return
        if (now - self._still_since[i]) < SETTLE_S:
            return
        kind = "old" if held else "new"
        if self.head[i] is None:
            self.head[i] = kind
            print("ch %d head=%s" % (i + 1, kind))
            
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
    
        if became and self.last_false[i] is not None:
            self.edge_at[i] = now
            print("%.3f ch %d EDGE" % (now, i + 1))
            send(self.events.make(self.sntp, "stroke", {"ch": i + 1}))
            
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
        self._settle(i, held, now)
        return kind, held


    def _tower_step(self):
        ringing = self.tower.tower in ("active", "ringing_down")
        if self.sntp is not None:
            self.sntp.set_ringing(ringing)
        self._prev_tower = self.tower.tower
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
            await self._start_ring(self.survey_state.kinds)
        await self._watch_empty(time.monotonic())
        self.guard.loop_tick()

    async def sense_task(self):  # or loop()
        while True:
            await self._sense_step()
            await asyncio.sleep(0.02)

    async def status_task(self):
        while True:
            # existing status / WS send
            await asyncio.sleep(0.1)
            
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

            # heartbeat beat (always toggles)
            heartbeat_on = (time.monotonic() % HEARTBEAT_LENGTH) < (HEARTBEAT_LENGTH /2) 

            # paint channels with channel_on; heartbeat uses heartbeat_on
            # simplest: call _paint with heartbeat_on so LED6 flashes,
            # and let panel_frame blank channels only when their own rate says so
            self._paint_kinds(kinds, heartbeat_on if rate == 0 else channel_on)
            await asyncio.sleep(0.05)
        
def start_app():
    """Everything up to the steady loop: net, Concentrator (survey,
    classify, self-test, ring), first time sync, server.  The watchdog is
    armed last."""
    ip = setup_net()
    app = Concentrator(cfg)
    app.link = "wifi_only" if ip else "no_wifi"   # TODO: ws_client from farm_ws

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
        app.status_task(),
        app.panel_task(),
        ws_poll_task(app.guard),
        app.refresher.run()
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
