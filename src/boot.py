# boot.py — pause only. Do not remount the USB disk.
import time
import sys

print("boot.py", sys.implementation.name)
time.sleep(8)

try:
    import usb_cdc
    import usb_midi
    usb_cdc.enable(console=True, data=True)
    usb_midi.enable()
    print("USB: REPL + data + MIDI")
except Exception as e:
    print("USB setup skipped:", e)
