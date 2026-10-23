# Survey "why": where the classification reason is shown

`classify_socket` returns `(kind, why)`. `why[0]` is one readable line ending with the measurement(s) that decided it, e.g. `(l50 0.12 < 0.20)`. `Concentrator._survey_settled` runs each time a survey settles.

| Where | What |
|---|---|
| Console / farm_log | One line per channel per settled survey: `ch 2 fault: Orange shorted to ground … (l50 0.00 < 0.20) (was empty)`. ` (was X)` only when the kind changed. Surveys run at boot, on plug changes, on an empty socket's new head, or on a 10 s trigger hold, so this is a few lines, not a stream. |
| `/status` | `parts.survey = {phase, channels: [{ch, kind, why[≤3], latched}]}`. `latched` is set when a channel becomes `fault`: `{why, seq, uptime_s, utc_ms}` of the `channel` event that recorded it. It is cleared when the channel leaves `fault`. |
| Heartbeat (`sand.panel/v1`, every 5 s, not stored) | `body.chan = [[kind, short_why or null] × n]`. The why is given only for fault / trigger / unknown: the first line cut to `WHY_HB_CHARS` (80) while keeping its trailing `(measurements)`. About 130–700 bytes. |
| Stored event | `sand.event/v1` type `channel`, `{ch, kind, prev_kind, why}`. Sent only when a channel's kind changes (so once per channel at boot, `prev_kind: "unknown"`), kept for replay like `link` events. |
