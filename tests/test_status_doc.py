# test_status_doc.py -- /status as versioned pieces (status_doc.py)
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from support import case, finish
import status_doc


def doc(ts=1, tower="inactive", sections=("network", "time", "heads", "tower")):
    return {"schema": "sand.status/v1", "id": "sandswing", "ts": ts, "level": "ok",
            "panel": [[0, 0, 0]] * 6,
            "sections": [{"id": s, "title": s.title(),
                          "items": [{"key": "v", "value": tower if s == "tower" else s}]}
                         for s in sections],
            "parts": {"link": {"drops": 0}, "ring": {"n": 1}}}


def built(d, now=0.0, parts=True):
    sd = status_doc.StatusDoc("b007")
    sd.refresh(now, lambda: d, 2.0, parts=parts)
    return sd


@case
def test_full_document_is_the_same_json_as_before_plus_version():
    d = doc()
    sd = built(d)
    out = json.loads(status_doc.join(sd.full()))
    for k in d:
        assert out[k] == d[k], k
    assert out["status_version"] == 1 and out["boot_id"] == "b007" and out["full"] is True
    lean = json.loads(status_doc.join(sd.full(parts=False)))
    assert "parts" not in lean and lean["sections"] == d["sections"]


@case
def test_version_moves_only_when_something_changed():
    sd = built(doc())
    assert sd.refresh(5.0, lambda: doc(), 2.0) and sd.version == 1      # same content
    assert not sd.refresh(6.0, lambda: doc(ts=2), 2.0)                  # within min_s
    sd.refresh(8.0, lambda: doc(ts=2, tower="active"), 2.0)
    assert sd.version == 2
    d = json.loads(status_doc.join(sd.delta(1)))
    assert d["delta"] is True and d["head"]["ts"] == 2
    assert [s["id"] for s in d["sections"]] == ["tower"] and "parts" not in d
    assert json.loads(status_doc.join(sd.delta(2)))["sections"] == []


@case
def test_a_removed_section_is_said_and_the_order_kept():
    sd = built(doc())
    sd.refresh(5.0, lambda: doc(sections=("network", "heads", "tower")), 2.0)
    d = json.loads(status_doc.join(sd.delta(1)))
    assert d["removed"] == ["time"] and d["order"] == ["network", "heads", "tower"]


@case
def test_knows_only_this_boot_and_versions_it_had():
    sd = built(doc())
    assert sd.knows(1, "b007") and not sd.knows(0, "b007")
    assert not sd.knows(1, "other") and not sd.knows(2, "b007") and not sd.knows(None, "b007")


@case
def test_pieces_are_unit_sized_never_the_whole_document():
    d = doc()
    d["sections"].append({"id": "big", "items": [{"value": "x" * 1200}]})
    sd = built(d)
    pieces = sd.full()
    assert max(len(p) for p in pieces) < 1400 < status_doc.size(pieces)
    assert sd.size_b == status_doc.size(pieces)


@case
def test_merging_deltas_rebuilds_the_full_document():
    """What Sandbells' page does with a delta (farm_status.js mergeStatus)."""
    sd = built(doc())
    base = json.loads(status_doc.join(sd.full(parts=False)))
    sd.refresh(5.0, lambda: doc(ts=3, tower="active", sections=("network", "heads", "tower", "extra")), 2.0)
    d = json.loads(status_doc.join(sd.delta(base["status_version"])))
    merged = dict(base)
    merged.update(d.get("head", {}))
    by_id = dict((s["id"], s) for s in base["sections"])
    for s in d["sections"]:
        by_id[s["id"]] = s
    merged["sections"] = [by_id[i] for i in d["order"]]
    merged["status_version"] = d["status_version"]
    want = json.loads(status_doc.join(sd.full(parts=False)))
    assert merged == want


@case
def test_parts_are_kept_only_when_asked_and_never_move_the_version():
    """16:18 'mem free 87 kB' (was 103-111): the parts were most of the cache
    and of each rebuild's garbage, and no delta carries them."""
    d = doc()
    d["parts"]["survey"] = {"pad": "s" * 3000}
    sd = status_doc.StatusDoc("b007")
    sd.refresh(0.0, lambda: d, 2.0)                            # a delta poller
    lean = sd.cache_b()
    assert not [k for k in sd.units if k.startswith("p:")] and lean < 1000, lean
    assert sd.refresh(1.0, lambda: d, 2.0, parts=True)         # a plain /status: built now
    full = json.loads(status_doc.join(sd.full()))
    assert full["parts"]["survey"] == d["parts"]["survey"] and sd.version == 1
    assert sd.cache_b() > lean + 3000
    d2 = doc()
    d2["parts"]["survey"] = {"pad": "t" * 3000}                # only parts changed
    sd.refresh(5.0, lambda: d2, 2.0)
    assert sd.version == 1 and sd.cache_b() == lean            # dropped again, version kept


finish()
