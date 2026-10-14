# WS2812/WS2813 test on GP16
# Paste into the REPL or save as code.py

import time
import board
import neopixel

NUM = 8          # change to match your strip length
PIXELS = neopixel.NeoPixel(board.GP16, NUM, brightness=0.3, auto_write=False)

def clear():
    for i in range(NUM):
        PIXELS[i] = (0, 0, 0)
    PIXELS.show()

def chase(color, delay=0.1):
    for i in range(NUM):
        clear()
        PIXELS[i] = color
        PIXELS.show()
        time.sleep(delay)

def fill(color):
    for i in range(NUM):
        PIXELS[i] = color
    PIXELS.show()

# --- exercise ---
clear()
time.sleep(0.5)

# single-pixel chase
chase((0, 64, 0))    # green
chase((64, 0, 0))    # red
chase((0, 0, 64))    # blue

# all on, then off
fill((32, 32, 0))
time.sleep(1)
clear()

print("NeoPixel test done")