""" panel.py -- the 6 front-panel LEDs (WS2813 on GP16), one function for all.
Practical rates (starting point)

rate        meaning             typical use

+ 0          solid                        normal kinds
+ 1       slow (~0.6 s)           warning / settling
+ 2        medium (~0.3 s)     fault
+ 3         fast (~0.2 s)             examining / active survey
+ 4  (-<)  very fast                   critical / knee-sweep etc.
#
# panel_frame() is the single source.  The strip, the NeoPixel rig and the
# ws payload all consume its list (spec L1).  Colours are RGB.
# TODO(spec L1-L3): full panel_frame(stage, link, sync, channels, bell_map,
# activity) with blink patterns and per-channel class/laser_duty/threshold.

panel.py -- the 6 front-panel LEDs (WS2813 on GP16), one function for all.
...
"""

import time

PANEL_N = 6

LED_KIND = {
    "loopback":  ((0, 0, 24), 0),
    "head_new":  ((0, 32, 0), 0),
    "head_old":  ((16, 32, 0), 0),
    "empty":     ((24, 12, 0), 0),
    "no_board":  ((0, 0, 0), 0),
    "fault":     ((48, 0, 0), 2),
    "unknown":   ((8, 8, 8), 0),
    "examining": ((0, 48, 48), 3),   # cyan, fast – while a channel is being surveyed
    "surveying": ((0, 48, 48), 1),   # slow cyan  (add this)
    "response":  ((48, 48, 48), 0),
}

LED_LINK = {"ws_client": (0, 32, 0), "wifi_only": (0, 0, 32),
            "no_wifi": (32, 12, 0), "error": (48, 0, 0)}
LED_ACTIVITY = {
    "heartbeat": (8, 8, 8),
    "heartbeat_nosync": (40, 0, 0),   # dim amber – no timing lock
    "ringing": (0, 32, 0),
    "ringing_down": (32, 16, 0), "selftest": (0, 24, 24),
    "watchdog_reset": (48, 0, 48)
    }

def _flash_on(rate, beat_on):
    """rate 0 = solid. Higher = more off-time."""
    if rate <= 0:
        return True
    return beat_on if (rate % 2) else not beat_on

def panel_frame(kinds, link="no_wifi", activity="heartbeat",
                beat_on=True, right=None):
    out = [{"rgb": LED_LINK.get(link, LED_LINK["error"]), "state": link}]
    for ch in range(4):
        k = kinds.get(ch, "unknown")
        colour, rate = LED_KIND.get(k, LED_KIND["unknown"])
        if not _flash_on(rate, beat_on):
            colour = (0, 0, 0)
        out.append({"rgb": colour, "state": k})
        
    a = LED_ACTIVITY.get(activity, LED_ACTIVITY["heartbeat"])
    if right is not None:
        a = right
        
    if not beat_on and activity in ("heartbeat", "heartbeat_nosync") and right is None:
        a = (0, 0, 0)
        
    out.append({"rgb": tuple(a), "state": activity})
    return out

# panel_pixels, paint, payload, StatusSlot unchanged from original


def panel_pixels(kinds, link="no_wifi", activity="heartbeat", beat_on=True,
                 right=None):
    return [e["rgb"] for e in panel_frame(kinds, link, activity, beat_on, right)]


def paint(strip, frame):
    """Write a frame to any 6+ pixel strip (GP16 strip or the rig)."""
    for i, e in enumerate(frame[:PANEL_N]):
        strip[i] = e["rgb"]
    strip.show()


def payload(frame):
    """ws fields: "panel": [[r,g,b] x6], "panel_state": [...]."""
    return {"panel": [list(e["rgb"]) for e in frame],
            "panel_state": [e["state"] for e in frame]}


class StatusSlot:
    """Stands in for tower_watch's status pixel: it remembers the colour
    tower_watch paints, so panel_frame can put it on LED6 instead of
    tower_watch writing the strip directly."""
    def __init__(self):
        self.colour = None

    def __setitem__(self, i, colour):
        self.colour = colour

    def show(self):
        pass
