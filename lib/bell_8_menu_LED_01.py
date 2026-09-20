# ==============================================
# 8-BELL MENU – FINAL + DEBUG TOGGLE – DEC 2025
# ==============================================

import time
import board
import busio
import digitalio
import usb_hid
import usb_midi
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keycode import Keycode
from adafruit_onewire.bus import OneWireBus
from adafruit_ds18x20 import DS18X20
#import adafruit_midi
#from adafruit_midi.note_on import NoteOn
#from adafruit_midi.note_off import NoteOff
import i2cencoderlibv21 as enc_lib

# ==============================================
# DEBUG CONTROL — CHANGE THIS ONE LINE ONLY
# ==============================================
DEBUG = True   # ← Set to False for silent operation, True for full debug spam
# ==============================================

def dprint(*args, **kwargs):
    if DEBUG:
        print("[DEBUG]", *args, **kwargs)

dprint("Script started")

# === OneWire ===

# Initialize one-wire bus on board pin GPI013.
ow_bus = OneWireBus(board.GP13)      # GPIO13 on Pi Pico 2W

dprint('SCAN:',ow_bus.scan())
devices = ow_bus.scan()

for i, d in enumerate(devices):
    print(f"Device {i:>3}")
    print("\tSerial Number = ", end="")
            
    for byte in d.serial_number:
        print(f"{byte:02x}", end ="")
        #\tROM=0x{:02x} \tFamily=0x{:02x}".format(d.serial_number, d.rom, d.family_code))
    print(f"\n\tFamily = 0x{d.family_code:02x}")
    
ds18 = DS18X20(ow_bus, ow_bus.scan()[0])         

dprint(f"Temperature: {ds18.temperature:0.3f}C")
time.sleep(1.0)

# Scan for sensors and grab the first one found.
ds18 = DS18X20(ow_bus, ow_bus.scan()[0])
dprint('Address:',ds18.address)

# === LED ===
led = digitalio.DigitalInOut(board.GP15)
led.direction = digitalio.Direction.OUTPUT

# === BELLS ===
bells = [
    {"pin": board.GP22, "key": Keycode.ONE,     "note": 41},
    {"pin": board.GP21, "key": Keycode.TWO,     "note": 40},
    {"pin": board.GP20, "key": Keycode.THREE,   "note": 38},
    {"pin": board.GP19, "key": Keycode.FOUR,    "note": 36},
    {"pin": board.GP18, "key": Keycode.FIVE,    "note": 34},
    {"pin": board.GP17, "key": Keycode.SIX,     "note": 33},
    {"pin": board.GP16, "key": Keycode.SEVEN,   "note": 31},
    {"pin": board.GP14, "key": Keycode.EIGHT,   "note": 29},
]

for b in bells:
    s = digitalio.DigitalInOut(b["pin"])
    s.pull = digitalio.Pull.UP
    b["sensor"] = s

enabled = [True] * 8
kbd = Keyboard(usb_hid.devices)
# midi = adafruit_midi.MIDI(midi_out=usb_midi.ports[1], out_channel=0)

# Direct access to the USB MIDI output port
midi_out = usb_midi.ports[1]

# MIDI channel 1 (channels are 0-15 in the protocol, so channel 0 here = MIDI channel 1)
channel = 0

# Status bytes for Note On and Note Off on the chosen channel
NOTE_ON  = 0x90 | channel
NOTE_OFF = 0x80 | channel

def ring(i):
    if not enabled[i]: 
        dprint(f"Bell {i+1} disabled")
        return
    dprint(f"RINGING BELL {i+1}")
    b = bells[i]
    kbd.press(b["key"])
    kbd.release_all()
    #midi.send(NoteOn(b["note"], 100))
    midi_out.write(bytearray([NOTE_ON, notes[i], velocity]))
    # Send Note Off (velocity 0 is standard for Note Off)
    midi_out.write(bytearray([NOTE_OFF, notes[i], 0]))
    #midi.send(NoteOff(b["note"]))

# === MENU & STATE ===
menu = [
    {"name": "Enable Bells", "rgb": 0xFF8800, "type": "toggle"},
    {"name": "Test Bells",   "rgb": 0x00FF00, "type": "trigger"},
    {"name": "All On",       "rgb": 0x00FFFF, "type": "all_on"},
    {"name": "Laser Test",   "rgb": 0xFF00FF, "type": "laser_seq"},  # ← NEW
]

in_test_mode = False
in_submenu   = False
menu_idx     = 0
bell_idx     = 0
last_counter = 0
button_down  = False
press_time   = 0

# Add a variable to track laser sequence state
laser_seq_step = 0  # 0=GP1 only, 1=GP2 only, 2=both, 3=none

onewire_reading = True     # enable one wire temperature sensing  May need turning off for ringing.
onewire_interval= 60       # interval between onewire reads in seconds.
conversion_ready_at = time.monotonic()

# === CALLBACKS WITH DEBUG ===
def on_rotate():
    global menu_idx, bell_idx, last_counter, laser_seq_step
    
    if not in_test_mode: 
        return
    
    encoder_read_counter = int.from_bytes(encoder.readCounter32(),'big')
    now = encoder_read_counter
    delta = now - last_counter
    last_counter = now
    
    if delta == 0: 
        return
    
    steps = abs(delta)
    direction = 1 if delta > 0 else -1
    
    dprint(f"Rotation {'Clockwise' if delta < 0 else 'Anti-Clockwise'}")
        
    if in_submenu:
        bell_idx = (bell_idx + direction * steps) % 8
        dprint(f"→ Bell {bell_idx+1}")
    else:
        old_idx = menu_idx
        old_type = menu[old_idx]["type"] if old_idx < len(menu) else None
        
        # Handle rotation within Laser Test
        if menu[menu_idx]["type"] == "laser_seq":
            laser_seq_step = (laser_seq_step + direction * steps) % 4
            update_laser_sequence(laser_seq_step)
        else:
            # Normal menu navigation
            menu_idx = (menu_idx + direction * steps) % len(menu)
            dprint(f"→ Menu: {menu[menu_idx]['name']}")
            encoder.write_rgb_code(menu[menu_idx]["rgb"])
        
        # === SELF-CONTAINED CLEANUP ===
        # If we just rotated AWAY from Laser Test → turn lasers off
        if old_type == "laser_seq" and menu[menu_idx]["type"] != "laser_seq":
            update_laser_sequence(None)  # Force cleanup
        
        # If we just arrived AT Laser Test → start sequence from step 0
        if menu[menu_idx]["type"] == "laser_seq" and old_idx != menu_idx:
            laser_seq_step = 0
            update_laser_sequence(0)  

def on_button_down():
    global button_down, press_time
    button_down = True
    press_time = time.monotonic()
    dprint("BUTTON DOWN")

def handle_long_press(duration):
    global in_test_mode, in_submenu, menu_idx, bell_idx
    
    if in_test_mode:
        # === EXITING TEST MODE ===
        # Clean up if we were in Laser Test
        if menu[menu_idx]["type"] == "laser_seq":
            update_laser_sequence(None)
        
        in_test_mode = False
        in_submenu = False
        menu_idx = bell_idx = 0
        
        dprint("EXIT TEST MODE – Lasers cleaned if needed")
        time.sleep(0.5)
        dprint("Back to normal ringing")
        
    else:
        # === ENTERING TEST MODE ===
        in_test_mode = True
        in_submenu = False
        menu_idx = bell_idx = 0
        laser_seq_step = 0
        
        encoder.write_rgb_code(0xFFFF00)  # solid yellow
        dprint("ENTER TEST MODE")

def enter_submenu():
    """First short press in test mode: highlight current menu item."""
    global in_submenu, bell_idx
    
    in_submenu = True
    bell_idx = 0
    c = menu[menu_idx]["rgb"]
    encoder.write_rgb_code(c)
    time.sleep(0.2)
    encoder.write_rgb_code(c // 2)
    dprint("Entered submenu")


def execute_current_menu_action():
    """Second short press: perform the action for the current menu item."""
    item = menu[menu_idx]
    
    if item["type"] == "toggle":
        enabled[bell_idx] = not enabled[bell_idx]
        encoder.write_rgb_code(0x00FF00 if enabled[bell_idx] else 0xFF0000)
        time.sleep(0.3)
        dprint(f"Bell {bell_idx+1} {'ENABLED' if enabled[bell_idx] else 'DISABLED'}")
        
    elif item["type"] == "trigger":
        ring(bell_idx)
        encoder.write_rgb_code(0x00FF00)
        time.sleep(0.3)
        dprint(f"Manual ring → bell {bell_idx+1}")
        
    elif item["type"] == "all_on":
        enabled[:] = [True] * 8
        encoder.write_rgb_code(0x00FFFF)
        time.sleep(0.5)
        dprint("ALL BELLS FORCED ON")


def exit_submenu():
    """Return to dimmed menu color after an action."""
    encoder.write_rgb_code(menu[menu_idx]["rgb"] // 2)
    global in_submenu
    in_submenu = False
def update_laser_sequence(step=None):
    global laser_seq_step
    
    if step is None:
        # Cleanup: force off
        encoder.writeGP1(LASER_OFF)
        encoder.writeGP2(LASER_OFF)
        laser_seq_step = 0
        dprint("Laser Test → CLEANED UP (both OFF)")
        return
    
    laser_seq_step = step % 4
    
    if laser_seq_step == 0:
        encoder.writeGP1(LASER_BRIGHT)
        encoder.writeGP2(LASER_OFF)
        dprint("Laser Test → GP1 only")
    elif laser_seq_step == 1:
        encoder.writeGP1(LASER_OFF)
        encoder.writeGP2(LASER_BRIGHT)
        dprint("Laser Test → GP2 only")
    elif laser_seq_step == 2:
        encoder.writeGP1(LASER_BRIGHT)
        encoder.writeGP2(LASER_BRIGHT)
        dprint("Laser Test → Both on")
    elif laser_seq_step == 3:
        encoder.writeGP1(LASER_OFF)
        encoder.writeGP2(LASER_OFF)
        dprint("Laser Test → Both off")
    
    encoder.write_rgb_code(menu[menu_idx]["rgb"])

def handle_short_press():
    """Handle short press behavior when already in test mode."""
    global in_submenu
    
    if not in_submenu:
        enter_submenu()
    else:
        execute_current_menu_action()
        exit_submenu()


# Main button up handler – now very clear and short
def on_button_up():
    global button_down
    
    if not button_down:
        return
    
    duration = time.monotonic() - press_time
    button_down = False
    dprint(f"BUTTON UP – held {duration:.2f}s")
    
    if duration >= 0.7:
        handle_long_press(duration)
    elif in_test_mode:
        handle_short_press()

# === ENCODER SETUP ===
dprint("Creating encoder...")

laser_intensity = 200
LASER_BRIGHT = 0
LASER_OFF = 255
LASER_DIM = 245
LASER_BRIGHTISH = 245

i2c = busio.I2C(board.GP5, board.GP4)
encoder = enc_lib.I2CEncoderLibV21(i2c, address=0x47)

encoder_IDcode = int.from_bytes(encoder.readIDCode())
encoder_Version = int.from_bytes(encoder.readVersion())
dprint("Encoder created ID:", encoder_IDcode, "Version:", encoder_Version )

# Wipe everything
dprint("Wiping callbacks...")

for n in ["onChange",
          "onButtonPush",
          "onButtonRelease",
          "onIncrement",
          "onDecrement",
          "onButtonLongPush",
          "onButtonDoublePush",
          "onButtonLongPress"
          ]:
    if hasattr(encoder, n):
        setattr(encoder, n, None)

# THE MAGIC LINES — THESE MAKE ROTATION WORK
encoder.onIncrement = on_rotate
encoder.onDecrement = on_rotate
encoder.onButtonPush = on_button_down
encoder.onButtonRelease = on_button_up

dprint("Callbacks assigned")

# Hardware init
config = (enc_lib.INT_DATA | enc_lib.WRAP_ENABLE | enc_lib.DIRE_LEFT |
          enc_lib.IPUP_ENABLE | enc_lib.RMOD_X1 | enc_lib.RGB_ENCODER)
encoder.begin(config)
encoder.write_antibounce_period(8)
encoder.write_double_push_period(30)
encoder.write_fade_rgb(0)

# ONLY ROTATION INTERRUPTS
encoder.setInterrupts(enc_lib.RINC | enc_lib.RDEC)

# DO NOT USE autoconfig_interrupt() — IT KILLS ROTATION
# encoder.autoconfig_interrupt()   ← DELETE THIS LINE

# All required to implement up down count.
encoder.write_counter(0)
encoder.write_max(35)
encoder.write_min(-20)
encoder.write_step_size(1)

gp1conf = enc_lib.GP_PWM | enc_lib.GP_PULL_DI | enc_lib.GP_INT_DI
gp2conf = enc_lib.GP_PWM | enc_lib.GP_PULL_DI | enc_lib.GP_INT_DI

# dprint('gp1conf:', gp1conf)
# dprint('gp2conf:', gp2conf)

encoder.writeGP1conf(gp1conf)
encoder.writeGP2conf(gp2conf)

def print_gpi_state():
    dprint('Laser1 %s'% (encoder.readGP1(),))
    dprint('Laser2 %s' % (encoder.readGP2(),))

    #print('Laser1 conf %s' % (encoder.readGP1conf(),))
    #print('Laser2 conf %s' % (encoder.readGP2conf(),))

# Rainbow test
encoder.writeGP1(LASER_BRIGHT) # Side Lasers on   255 off 0 full brightness
encoder.writeGP2(LASER_BRIGHT) # Front Laser on

dprint("Rainbow test...")
for c in [
            0xFF0000,
            0xFF8000,
            0xFFFF00,
            0x00FF00,
            0x00FFFF,
            0x0000FF,
            0xFF00FF
        ]:
    encoder.write_rgb_code(c)
    time.sleep(0.25)
    
encoder.write_rgb_code(0x000000)

encoder.writeGP1(LASER_OFF) # Side Lasers off   255 off 0 full brightness
encoder.writeGP2(LASER_OFF) # Front Laser off

print("\nSYSTEM FULLY ARMED – Hold encoder 0.7s to enter test mode")
print("DEBUG =", DEBUG, "← change to False for silent mode\n")


# === MAIN LOOP ===
try:
    while True:
        encoder.update_status()
        encoder.write_fade_rgb(0)

        # === FORCE LED BEHAVIOR BASED ON MODE ===
        if not in_test_mode:
            # Normal operation — everything off
            encoder.write_rgb_code(0x000000)
            # <<< KEEP THIS – ensures lasers stay off in normal mode (including after boot) >>>
            encoder.writeGP1(LASER_OFF)
            encoder.writeGP2(LASER_OFF)
        else:
            # In test mode – menu controls ring color, Laser Test controls GP1/GP2
            if button_down:
                hold_time = time.monotonic() - press_time
                if hold_time >= 0.7:
                    encoder.write_rgb_code(0x000000)
                elif hold_time >= 0.4:
                    encoder.write_rgb_code(0x331100)
            else:
                if in_submenu:
                    encoder.write_rgb_code(menu[menu_idx]["rgb"] // 2)
                else:
                    encoder.write_rgb_code(menu[menu_idx]["rgb"] // 2)

        # Live long-press feedback (same as before)
        if button_down and (time.monotonic() - press_time >= 0.7):
            if in_test_mode:
                encoder.write_rgb_code(0x000000)
            else:
                encoder.write_rgb_code(0xFFFF00)
        elif button_down and (time.monotonic() - press_time >= 0.4):
            if not in_test_mode:
                encoder.write_rgb_code(0x444400)

        # LED heartbeat
        led.value = (int(time.monotonic() * 8) % 2) == 0 if in_test_mode else (int(time.monotonic()) % 2) == 0

        # Normal ringing
        if not in_test_mode:
            for i in range(8):
                if not bells[i]["sensor"].value:
                    ring(i)
                    while not bells[i]["sensor"].value:
                        time.sleep(0.005)

        time.sleep(0.01)
        
        if  time.monotonic() > conversion_ready_at and onewire_reading:  
            
            conversion_delay = ds18.start_temperature_read()
            # dprint('Conversion_delay:', conversion_delay)
            time_mono = time.monotonic()
            conversion_ready_at =  time_mono + conversion_delay + onewire_interval
            # dprint('Time mono-', time_mono,'Conversion_ready_at:', conversion_ready_at)
            temp = ds18.read_temperature()
            print('Average', 21, 'temp:', temp)
            #print(f"\nTemperature: {temp:0.3f}C\n")

#         time.sleep(2)
#         dprint('Round the loop')

finally:
    # === GRACEFUL SHUTDOWN CLEANUP ===
    # This runs on KeyboardInterrupt (Ctrl+C), power off, or any exception
    dprint("Shutdown detected – turning off all encoder lights")
    encoder.write_rgb_code(0x000000)   # Ring LED off
    encoder.writeGP1(LASER_OFF)        # Side lasers off
    encoder.writeGP2(LASER_OFF)        # Front laser off
    encoder.write_fade_rgb(0)
    
    # Optional: small delay to ensure I2C commands are sent
    time.sleep(0.1)
    
    print("All lights OFF – safe shutdown complete")