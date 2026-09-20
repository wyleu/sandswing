# boot.py

#import network
#import webrepl
import time
import json
import storage

# boot.py – USB configuration (runs early)
import sys
import os

storage.remount("/", readonly=False)   # False = CircuitPython can write

print("boot.py running on", sys.implementation.name)

if sys.implementation.name == "circuitpython":
    try:
        import usb_cdc
        import usb_midi

        # Enable REPL console + extra data serial port (your "Com port" for abell ASCII)
        usb_cdc.enable(console=True, data=True)

        # Enable USB MIDI device
        usb_midi.enable()

        print("CircuitPython: Enabled REPL + extra serial + USB MIDI")
    except Exception as e:
        print("CircuitPython USB setup failed:", e)

elif sys.implementation.name == "micropython":
    # MicroPython fallback: REPL only by default
    # Extra CDC + MIDI requires custom TinyUSB code or mip package (advanced)
    # For now: just print (no extra devices)
    print("MicroPython: Using default REPL only (no extra USB serial/MIDI)")
    # If you install usb-device-midi via mip and have a custom example:
    # try: import usb_device_midi_example; usb_device_midi_example.enable()
    # except: pass

# Optional: common setup (e.g. disable mass storage if desired – careful!)
# import storage
# storage.disable_usb_drive()  # Uncomment only if you know what you're doing!

# These will be globals → visible in main.py
config = {}
enabled_formats = []
output_conf = {}

test_conf = config.get("test", {})
TEST_ENABLED = test_conf.get("enabled", False)
TEST_MODE = test_conf.get("mode", "midi")          # "midi", "all", "abell", "log", etc.
TEST_INTERVAL_MS = test_conf.get("interval_ms", 5000)

test_midi_note = test_conf.get("midi", {}).get("note", 60)
test_midi_vel  = test_conf.get("midi", {}).get("velocity", 100)
test_midi_dur  = test_conf.get("midi", {}).get("duration_ms", 200)

# Fallbacks...
FALLBACK_SSID = "NOWRH81L"
FALLBACK_PASS = "w3JzkYiA9UNv"
# ... other fallbacks



try:
    with open("settings.json", "r") as f:
        config = json.load(f)
    print("Loaded settings.json")

    # Extract what main.py needs
    output_conf = config.get("output", {})
    enabled_formats = output_conf.get("enabled_formats", [])

    # WiFi stuff using config...
    wifi = config.get("wifi", {})
    WIFI_SSID = wifi.get("ssid", FALLBACK_SSID)
    WIFI_PASS = wifi.get("password", FALLBACK_PASS)
    # ... rest of WiFi connect code

#     # WebREPL...
#     if config.get("webrepl", {}).get("enabled", True):
#         pw = config.get("webrepl", {}).get("password", "letmein")
#         webrepl.start(password=pw)
#         print("WebREPL started")

except Exception as e:
    print("settings.json error:", e)
    # Set defaults so main.py doesn't crash
    enabled_formats = ["log"]  # or [] or whatever safe default
    output_conf = {}

# Optional: print for debug (visible in REPL / WebREPL)
print("Globals set for main.py:")
print("enabled_formats:", enabled_formats)
print("output_conf keys:", list(output_conf.keys()))
