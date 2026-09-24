# main.py - Loads and runs the program specified in config.json → startup → program

import sys
import os
import time


DEFAULT_PROGRAM = None  # ← set to e.g. "fallback.py" if desired
CONFIG_FILE = "settings.json"
try:
    #import tomllib
    # with open("settings.toml", "rb") as f:
    #     config = tomllib.load(f)
    import json
    with open(CONFIG_FILE, "rb") as f:
        config = json.load(f)

    # Access examples
    wifi_ssid       = config["wifi"]["ssid"]
    midi_channel    = config["output"]["midi"]["channel"]
    output_conf     = config["output"]
    enabled_formats = config["output"]["enabled_formats"]
    test_enabled    = config["output"]["test"]["enabled"]
    startup_program = config["startup"]["program"]



except Exception as e:
    print("Could not load settings.json → using defaults")
    print(e)
    # fallback values
    wifi_ssid = "NOWRH81L"
    midi_channel = 0
    enabled_formats = ["log"]
    
try:
    import build_info
    print("CIRCUITPY", build_info.GIT, build_info.BUILT)
except Exception:
    print("CIRCUITPY unstamped")

print("WiFi SSID:", wifi_ssid)
print("MIDI channel:", midi_channel)
print("Enabled formats:", enabled_formats)
print("Test mode:", "on" if test_enabled else "off")
print("Program:",startup_program)

def get_start_program():
    try:
        if CONFIG_FILE not in os.listdir():
            print(f"No {CONFIG_FILE} found.")
            return None

        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)

        startup = config.get("startup", {})
        program = startup.get("program")

        if not program:
            print("No 'startup.program' key in config.json")
            return None

        if program not in os.listdir():
            print(f"Program file not found: '{program}'")
            print("Available:", os.listdir())
            return None

        return program
    
    except ValueError as e:
        print(f"JSON parse error in {CONFIG_FILE}: {e}")
    except OSError as e:
        print(f"Filesystem/IO error: {e}")
    except Exception as e:
        print(f"Config read error: {e}")
    
    return None

# ────────────────────────────────────────────────

start_file = get_start_program()

if start_file:
    print(f"Launching: {start_file}")
    time.sleep(0.5)  # let messages appear cleanly

    try:
        with open(start_file, "r") as f:
            code = f.read()
        
        exec(code, globals(), locals())
        print(f"→ {start_file} completed (or running in background)")
    
    except SyntaxError as e:
        print(f"Syntax error in {start_file}: {e}")
    except Exception as e:
        print(f"Error running {start_file}: {e}")
        sys.print_exception(e)

else:
    print("\nNothing to auto-start. REPL ready for manual run.")
    print("Example: exec(open('pico_microp_8bell_scan_rs_latch.py').read())")
    while True:
        time.sleep(10)  # keep alive (remove if you prefer immediate REPL prompt)