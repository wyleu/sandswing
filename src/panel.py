# panel.py -- the 6 front-panel LEDs (WS2813 on GP16), one function for all.
#
#   index 0      LED1 left status: link (+ time sync, TODO spec L3)
#   index 1..4   LED2-5 under sockets 1-4 (channel index + 1)
#   index 5      LED6 right status: tower / activity
#
# panel_frame() is the single source.  The strip, the NeoPixel rig and the
# ws payload all consume its list (spec L1).  Colours are RGB.
# TODO(spec L1-L3): full panel_frame(stage, link, sync, channels, bell_map,
# activity) with blink patterns and per-channel class/laser_duty/threshold.
PANEL_N = 6
LED_KIND = {
    "loopback": (0, 0, 24),      # blue: known blank / plug
    "head_new": (0, 32, 0),      # green
    "head_old": (16, 32, 0),     # yellow-green
    "empty":    (24, 12, 0),     # amber: nothing fitted, fit a blank
    "no_board": (0, 0, 0),       # off (only for 5-8; they have no panel LED)
    "fault":    (48, 0, 0),      # red (blink in firmware)
    "unknown":  (8, 8, 8),       # dim white: classifying, drive OFF
    "response": (48, 48, 48),    # bright white: head tripped (stroke)
}
LED_LINK = {"ws_client": (0, 32, 0), "wifi_only": (0, 0, 32),
            "no_wifi": (32, 12, 0), "error": (48, 0, 0)}
LED_ACTIVITY = {"heartbeat": (8, 8, 8), "ringing": (0, 32, 0),
                "ringing_down": (32, 16, 0), "selftest": (0, 24, 24),
                "watchdog_reset": (48, 0, 48)}


def panel_frame(kinds, link="no_wifi", activity="heartbeat", beat_on=True,
                right=None):
    """kinds: {ch: kind} for ch 0..3.  right: optional colour for LED6
    (e.g. tower_watch's own status colour).  -> 6 x {"rgb", "state"}."""
    out = [{"rgb": LED_LINK.get(link, LED_LINK["error"]), "state": link}]
    for ch in range(4):
        k = kinds.get(ch, "unknown")
        out.append({"rgb": LED_KIND.get(k, LED_KIND["unknown"]), "state": k})
    a = LED_ACTIVITY.get(activity, LED_ACTIVITY["heartbeat"])
    if right is not None:
        a = right
    if not beat_on and activity == "heartbeat" and right is None:
        a = (0, 0, 0)
    out.append({"rgb": tuple(a), "state": activity})
    return out


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
