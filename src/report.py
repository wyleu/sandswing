# report.py -- assembles the ws/status payload from parts' status() dicts.
# Only concentrator.py calls this; only concentrator.py talks to farm_ws.
from sand_status import from_concentrator


def build(facts, parts, panel_fields=None):
    """facts: the flat dict sand_status already understands.
    parts: {"tower_mux": tower.status(), "gate": gate.status(), ...}.
    Adds them under "parts", plus the panel fields."""
    doc = from_concentrator(facts)
    doc["parts"] = dict((k, v) for k, v in parts.items() if v is not None)
    if panel_fields:
        doc.update(panel_fields)
    return doc


def part(obj, method="status"):
    """obj.status() or None if obj is missing (e.g. no Wi-Fi -> no sntp)."""
    if obj is None:
        return None
    fn = getattr(obj, method, None)
    return fn() if fn is not None else None
