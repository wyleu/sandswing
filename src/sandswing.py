# sandswing.py — optical lineup + optional 8-bell rounds jabber
import asyncio
import time
import math
import board
import pwmio
import analogio
import wifi
import farm_log
from config_loader import load_config
from hardware_config import create_hardware
from farm_ws import connect_wifi, start_server, poll, send

cfg = load_config()
cfg.banner()

hw = create_hardware(cfg)
inputs = hw["inputs"]
leds = hw["leds"]
ir_pwm = hw["ir_pwm"]
tx_led = hw["tx_led"]
status_led = hw["status_led"]
pins = hw["pins"]
det = hw["detection"]
NUM = min(int(det.get("num_bells", len(inputs))), len(inputs), len(leds))
SWEEP_STEPS = cfg.get_int("sensing.test.sweep_steps", 20)
SETTLE_S = cfg.get_float("sensing.test.settle_s", 0.1)
REPEATS = cfg.get_int("sensing.test.sweep_repeats", 2)
COLOR_A = (0, 64, 0)
COLOR_B = (64, 0, 0)
STATUS = (0, 32, 0)
JABBER = cfg.get_bool("streams.bell.test_sweep", False)
ROUNDS_N = cfg.get_int("streams.bell.rounds_bells", 8)
BLOW_S = cfg.get_float("streams.bell.blow_s", 0.32)
GAP_S = cfg.get_float("streams.bell.gap_s", 0.32)
PERIOD = ROUNDS_N * BLOW_S + GAP_S
neo_phase = [False] * NUM

LASER_GPS = [pins["output_base"] + i for i in range(NUM)]
for ch in range(NUM):
    try:
        leds[ch].deinit()
    except Exception:
        pass
laser_pwms = []
for gp in LASER_GPS:
    laser_pwms.append(
        pwmio.PWMOut(getattr(board, "GP%d" % gp), frequency=1000, duty_cycle=0)
    )

def laser_duty(ch, duty):
    duty = max(0, min(65535, int(duty)))
    for o in range(NUM):
        laser_pwms[o].duty_cycle = duty if o == ch else 0

def lasers_all_off():
    for p in laser_pwms:
        p.duty_cycle = 0

adc_laser = []
try:
    adc_laser = [analogio.AnalogIn(board.GP26), analogio.AnalogIn(board.GP27)]
    print("ADC A0/A1 GP26/GP27")
except Exception as e:
    print("ADC laser skip:", e)
    adc_laser = []

adc_pt = None
try:
    adc_pt = analogio.AnalogIn(board.GP28)
    print("ADC A2 GP28")
except Exception as e:
    print("ADC A2 skip:", e)

def read_laser_current(ch):
    if not adc_laser or ch >= len(adc_laser):
        return None
    try:
        return adc_laser[ch].value
    except Exception:
        return None

def read_pt_v():
    if adc_pt is None:
        return None
    try:
        return adc_pt.value
    except Exception:
        return None

neo = None
status_color = STATUS
if cfg.format_enabled("neopixel"):
    try:
        import neopixel
        neo_conf = cfg.output.get("neopixel") or {}
        pin_n = pins.get("neopixel", neo_conf.get("pin", 16))
        bright = float(neo_conf.get("brightness", 0.25))
        order = getattr(neopixel, str(neo_conf.get("pixel_order", "GRB")).upper(), neopixel.GRB)
        status_color = tuple(neo_conf.get("status_color", list(STATUS)))
        neo = neopixel.NeoPixel(
            getattr(board, "GP%d" % pin_n),
            NUM + 1,
            brightness=bright,
            auto_write=False,
            pixel_order=order,
        )
        neo[0] = status_color
        for i in range(1, len(neo)):
            neo[i] = (0, 0, 0)
        neo.show()
        print("neopixel GP%d" % pin_n)
    except Exception as e:
        print("neopixel failed:", e)
        neo = None

def neo_select(ch):
    if not neo:
        return
    neo[0] = status_color
    for i in range(1, len(neo)):
        neo[i] = (16, 16, 16) if (i == ch + 1) else (0, 0, 0)
    neo.show()

def neo_toggle_channel(ch):
    if not neo:
        return
    neo_phase[ch] = not neo_phase[ch]
    neo[0] = status_color
    neo[ch + 1] = COLOR_B if neo_phase[ch] else COLOR_A
    neo.show()

def emit(msg):
    print(msg)
    farm_log.write(msg)

farm_log.start(cfg)

def read_pt(ch):
    return not inputs[ch].value

print("Optical lineup", "ch", NUM, "steps", SWEEP_STEPS)

def status_payload():
    ip = None
    try:
        ip = str(wifi.radio.ipv4_address)
    except Exception:
        pass
    return {
        "name": cfg.device_name,
        "family": cfg.family,
        "role": getattr(cfg, "role", None),
        "ip": ip,
        "mode": "rounds_jabber" if JABBER else "lineup",
        "jabber": JABBER,
        "num": NUM,
        "ws": True,
    }

def emit_tape(bell, u, amp=1.0, quiet=False):
    if not cfg.get_bool("streams.bell.enabled", False):
        return
    send({
        "type": "bell",
        "bell": int(bell),
        "u": float(u),
        "amp": float(amp),
        "t": time.monotonic(),
        "test": True if JABBER else False,
    })
    if not quiet:
        emit("WS bell=%d u=%.2f" % (bell, u))

async def rounds_jabber_task():
    last_u = [None] * (ROUNDS_N + 1)
    emit("WS ROUNDS 1..%d blow=%.2f gap=%.2f" % (ROUNDS_N, BLOW_S, GAP_S))
    while True:
        phase = time.monotonic() % PERIOD
        for b in range(1, ROUNDS_N + 1):
            local = phase - (b - 1) * BLOW_S
            if 0.0 <= local < BLOW_S:
                u = 2.5 + 2.4 * math.sin((local / BLOW_S) * math.pi)
            else:
                u = 2.5
            if last_u[b] is None or abs(u - last_u[b]) > 0.08:
                emit_tape(b, u, 1.0 if u > 2.6 else 0.0, quiet=True)
                last_u[b] = u
        poll()
        await asyncio.sleep(0.04)

def setup_net():
    if not connect_wifi(cfg):
        emit("No WiFi – lineup still runs")
        return
    start_server(cfg, status_payload)
    if JABBER:
        emit("WS ROUNDS jabber 1..%d" % ROUNDS_N)
    else:
        emit("WS live")

async def ws_poll_task():
    while True:
        poll()
        await asyncio.sleep(0.05)

async def test_channel(ch):
    emit("--- ch %d laser GP%d PT GP%d ---" % (ch + 1, LASER_GPS[ch], pins["input_base"] + ch))
    neo_select(ch)
    last = read_pt(ch)
    changes = 0
    curve = []
    thresholds = []
    for step in range(SWEEP_STEPS + 1):
        duty = int(65535 * step / SWEEP_STEPS)
        for _ in range(REPEATS):
            laser_duty(ch, duty)
            await asyncio.sleep(SETTLE_S)
            raw = read_pt(ch)
            i_sense = read_laser_current(ch)
            pt_v = read_pt_v()
            emit(
                "ch %d duty=%5d (%3.0f%%) PT=%s PT_v=%s I=%s"
                % (ch + 1, duty, 100.0 * duty / 65535.0, raw,
                   pt_v if pt_v is not None else "n/a",
                   i_sense if i_sense is not None else "n/a")
            )
            if raw != last:
                last = raw
                changes += 1
                neo_toggle_channel(ch)
                if pt_v is not None:
                    thresholds.append(pt_v)
                emit("ch %d THRESHOLD duty=%d PT→%s" % (ch + 1, duty, raw))
            curve.append((duty, 1 if raw else 0))
    lasers_all_off()
    active = [d for d, s in curve if s]
    if active:
        emit("ch %d first_PT_True~%d changes=%d" % (ch + 1, active[0], changes))
    else:
        emit("ch %d never PT_True changes=%d" % (ch + 1, changes))
    return changes

async def lineup_loop():
    status_led.value = True
    lasers_all_off()
    try:
        ir_pwm.duty_cycle = 0
    except Exception:
        pass
    emit("LINEUP START %s %s" % (cfg.family, cfg.device_name))
    while True:
        totals = []
        for ch in range(NUM):
            totals.append(await test_channel(ch))
        emit("PASS %s" % (totals,))

async def main():
    setup_net()
    tasks = [lineup_loop(), ws_poll_task()]
    if JABBER:
        tasks.append(rounds_jabber_task())
    await asyncio.gather(*tasks)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped")
finally:
    lasers_all_off()
    try:
        ir_pwm.duty_cycle = 0
    except Exception:
        pass
    if neo:
        for i in range(len(neo)):
            neo[i] = (0, 0, 0)
        neo.show()
    farm_log.close()
    print("Cleanup complete")