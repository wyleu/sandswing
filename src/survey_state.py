# survey_state.py -- generic survey/panel lifecycle.
# Any program can own one and call step()/trigger().
# One clean assessment pass; no nested retries.

import time

# Phases

# phase → kind to show (None = use last survey result)
PHASE_KIND = {
    "idle":       None,
    "examining":  "examining",
    "surveying":  "surveying",
    "settled":    None,
}

IDLE = "idle"
EXAMINING = "examining"
SURVEYING = "surveying"
SETTLED = "settled"

class SurveyState:
    """
    Drive a categorical device survey and its panel indication.
    Caller supplies:
      - survey_fn() -> {ch: {"kind": ..., "why": ..., "m": ...}}
      - paint_fn(kinds_or_phase, beat_on)
      - n_channels
    """

    def __init__(self, n_channels, survey_fn, paint_fn, flash_ticks=3):
    
        self.n = n_channels
        self.survey_fn = survey_fn
        self.paint_fn = paint_fn
        self.flash_ticks = flash_ticks
        self.phase = IDLE
        self.kinds = {}
        self.last_result = {}
        self._flash_left = 0
        self._doubt = False          # set when we should re-assess

    def trigger(self, reason="manual", channel=None):
        """Call when there is doubt (plug event, inactive, boot, explicit)."""
        self._doubt = True
        self._pending_ch = channel
        self._reason = reason


    async def step(self, beat_on=True):
        prev = self.phase
        if self.phase == IDLE and self._doubt:
            self.phase = EXAMINING
            self._doubt = False
        elif self.phase == EXAMINING:
            if not hasattr(self, "_exam_left"):
                self._exam_left = 15
            self._exam_left -= 1
            if self._exam_left <= 0:
                self.phase = SURVEYING
                del self._exam_left
        elif self.phase == SURVEYING:
            self.last_result = await self.survey_fn() or {}
            self.kinds = {c: r.get("kind", "unknown")
                          for c, r in self.last_result.items()}
            self.phase = SETTLED
        elif self.phase == SETTLED:
            self.phase = IDLE
            self._pending_ch = None
        if self.phase != prev:
            print("%.3f survey phase %s -> %s" % (time.monotonic(), prev, self.phase))
        return self.phase



    def _examining_kinds(self):
        if getattr(self, "_pending_ch", None) is not None:
            k = dict(self.kinds)
            k[self._pending_ch] = "examining"
            return k
        return {i: "examining" for i in range(self.n)}

    def status(self):
        return {"phase": self.phase, "kinds": dict(self.kinds),
                "doubt": self._doubt}