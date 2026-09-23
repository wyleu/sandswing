"""
test_strip.py — geometry of fake_strip.py (not the decoder yet).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fake_strip import strip_edges, samples

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok ", name)
    else:
        fails.append(name)
        print("FAIL", name, detail)


def test_cw_edges():
    e = list(strip_edges(70, "cw"))
    check("cw edges", e == [(0, True), (20, False), (30, True), (70, False)], e)


def test_acw_edges():
    e = list(strip_edges(70, "acw"))
    check("acw edges", e == [(0, True), (40, False), (50, True), (70, False)], e)


def test_cw_units_in_samples():
    s = list(samples(strip_edges(70, "cw"), step_ms=10, t_end=70))
    # t=0,10 shine; 20 dark; 30,40,50,60 shine; 70 off
    shine = [t for t, w in s if w]
    check("cw shine times", shine == [0, 10, 30, 40, 50, 60], shine)


if __name__ == "__main__":
    for fn in (test_cw_edges, test_acw_edges, test_cw_units_in_samples):
        print(fn.__name__)
        fn()
    print()
    if fails:
        print("FAILED:", ", ".join(fails))
        sys.exit(1)
    print("strip geometry passed")