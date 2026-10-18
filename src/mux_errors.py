# mux_errors.py -- exceptions shared by the mux/drive modules. Leaf.


class MuxConfigError(ValueError):
    """Bad pins/select settings, found before anything is claimed."""


class ChannelBusy(Exception):
    """Another bell holds the select lines."""


class NotSelected(Exception):
    """A bell path was used without holding the tower selection."""


class ThresholdMismatch(Exception):
    """GP28 readback of the filtered threshold disagrees with the duty set."""


class PwmSliceError(MuxConfigError):
    """Two PWM pins collide on one RP2350 slice/channel, or ask one slice
    for two frequencies."""
