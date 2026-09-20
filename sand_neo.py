# sand_neo.py
#
# Shared NeoPixel status + per-channel R/G toggle (Sand* farm)
# ------------------------------------------------------------
# Filename: sand_neo.py
#
# Overview
# --------
# pixel 0  = data-alive / status colour
# pixel 1..N = one LED per channel; toggles red/green on each
#              sensor change (same idea as Sandsense tick LED)
#
# Typical use
# -----------
#   from sand_neo import SandNeo
#   neo = SandNeo.from_cfg(cfg, pins, num_channels)
#   neo.select(0)
#   neo.toggle_channel(0)
#   neo.all_off()
#
# Requires neopixel library on CIRCUITPY.

COLOR_GREEN = (0, 64, 0)
COLOR_RED = (64, 0, 0)
COLOR_STATUS_DEFAULT = (0, 32, 0)
COLOR_DIM = (16, 16, 16)

class SandNeo:
    def __init__(self, strip, status_color=COLOR_STATUS_DEFAULT,
                 color_a=COLOR_GREEN, color_b=COLOR_RED):
        self.strip = strip
        self.status_color = status_color
        self.color_a = color_a
        self.color_b = color_b
        self.phase = [False] * (len(strip) - 1 if strip else 0)
        if strip:
            strip[0] = status_color
            for i in range(1, len(strip)):
                strip[i] = (0, 0, 0)
            strip.show()

    @classmethod
    def from_cfg(cls, cfg, pins, num_channels):
        """Build from config_loader Config + pins dict. Returns None if disabled/fail."""
        if not cfg.format_enabled("neopixel"):
            print("neopixel: not in enabled_formats")
            return None
        try:
            import board
            import neopixel
        except ImportError:
            print("neopixel: library missing")
            return None

        neo_conf = (cfg.output.get("neopixel") if cfg.output else None) or {}
        if neo_conf.get("enabled") is False:
            print("neopixel: disabled in settings")
            return None

        pin_n = pins.get("neopixel", neo_conf.get("pin", 15))
        bright = float(neo_conf.get("brightness", 0.25))
        order_name = str(neo_conf.get("pixel_order", "GRB")).upper()
        order = getattr(neopixel, order_name, neopixel.GRB)
        status = tuple(neo_conf.get("status_color", list(COLOR_STATUS_DEFAULT)))
        # optional overrides for R/G toggle
        color_a = tuple(neo_conf.get("tick_a", list(COLOR_GREEN)))
        color_b = tuple(neo_conf.get("tick_b", list(COLOR_RED)))

        try:
            strip = neopixel.NeoPixel(
                getattr(board, "GP%d" % pin_n),
                num_channels + 1,
                brightness=bright,
                auto_write=False,
                pixel_order=order,
            )
            print("neopixel GP%d (%d pixels)" % (pin_n, num_channels + 1))
            return cls(strip, status_color=status, color_a=color_a, color_b=color_b)
        except Exception as e:
            print("neopixel failed:", e)
            return None

    def select(self, ch):
        """Highlight channel under test (dim white); others off."""
        if not self.strip:
            return
        self.strip[0] = self.status_color
        for i in range(1, len(self.strip)):
            self.strip[i] = COLOR_DIM if (i == ch + 1) else (0, 0, 0)
        self.strip.show()

    def toggle_channel(self, ch):
        """Flip channel pixel red/green — sensor change seen."""
        if not self.strip or ch < 0 or ch >= len(self.phase):
            return
        self.phase[ch] = not self.phase[ch]
        self.strip[0] = self.status_color
        self.strip[ch + 1] = self.color_b if self.phase[ch] else self.color_a
        self.strip.show()

    def all_off(self):
        if not self.strip:
            return
        for i in range(len(self.strip)):
            self.strip[i] = (0, 0, 0)
        self.strip.show()