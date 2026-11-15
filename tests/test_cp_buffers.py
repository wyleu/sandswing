# test_cp_buffers.py -- firmware must use only the bytearray / memoryview / array
# operations CircuitPython 10.0.3 has.  CPython has more (slice deletion,
# .clear(), .pop(), .strip() ...), so a CPython-only op passes every other
# test here and then fails on the Pico.  tune.py did `del self._buf[:i + 1]`:
# a TypeError on the Pico, caught, so every inbound command line was dropped.
# SerialTune with a CircuitPython-like bytearray: test_tune.py (CPBytearray).
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from support import case, finish  # noqa: E402
import hb_slim  # noqa: E402
import serial_status  # noqa: E402

BUFFER_TYPES = ("bytearray", "memoryview", "array")
# methods each type has in CircuitPython 10.0.3 (py/objarray.c locals with
# MICROPY_CPYTHON_COMPAT; bytearray also gets bytes' decode / find family)
CP_METHODS = {
    "bytearray": frozenset(("append", "extend", "decode", "find", "rfind",
                            "index", "rindex")),
    "memoryview": frozenset(("cast", "hex")),
    "array": frozenset(("append", "extend")),
}


def buffer_names(tree):
    """{source text of an assignment target: buffer type} for targets bound
    to bytearray(...) / memoryview(...) / array(...) anywhere in the module."""
    names = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            f = n.value.func
            kind = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if kind in BUFFER_TYPES:
                for t in n.targets:
                    names[ast.unparse(t)] = kind
    return names


def cp_problems(source, fname="<src>"):
    """Lines using a buffer op CircuitPython 10.0.3 lacks: any `del` of an
    item / slice of a buffer, or a method outside CP_METHODS."""
    tree = ast.parse(source)
    bufs = buffer_names(tree)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Delete):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and ast.unparse(t.value) in bufs:
                    out.append("%s:%d del %s" % (fname, n.lineno, ast.unparse(t)))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            base = ast.unparse(n.func.value)
            kind = bufs.get(base)
            if kind is not None and n.func.attr not in CP_METHODS[kind]:
                out.append("%s:%d %s.%s() on a %s" % (fname, n.lineno, base, n.func.attr, kind))
    return out


@case
def test_detector_catches_the_tune_bug_and_spares_lists():
    bad = ("class T:\n    def __init__(self):\n        self._buf = bytearray()\n"
           "    def f(self, i):\n        del self._buf[:i + 1]\n")
    assert len(cp_problems(bad)) == 1, cp_problems(bad)
    assert cp_problems("b = bytearray(4)\nb.clear()\n")
    assert cp_problems("m = memoryview(b'ab')\nm.tobytes()\n")
    ok = ("x = []\ndel x[:2]\nb = bytearray()\nb = b[3:]\nb.extend(b'a')\n"
          "i = b.find(b'\\n')\n")
    assert cp_problems(ok) == [], cp_problems(ok)


@case
def test_firmware_uses_only_cp_buffer_ops():
    found = []
    for fn in sorted(os.listdir(SRC)):
        if fn.endswith(".py"):
            with open(os.path.join(SRC, fn)) as f:
                found += cp_problems(f.read(), fn)
    assert not found, "CPython-only buffer ops (CircuitPython 10.0.3 lacks them):\n  " + \
        "\n  ".join(found)


class FakeData:
    connected = True

    def write(self, buf):
        return len(buf)


@case
def test_serial_heartbeat_carries_tune_counters_cut_short():
    long_err = "TypeError: " + "x" * (3 * hb_slim.HB_UDP_STR_MAX)
    brief = {"lines": 7, "bad": 1, "errors": 2, "last_error": long_err}
    doc = {"type": "heartbeat", "body": {"tune": brief, "mem_free": 1}}
    ss = serial_status.SerialStatus(FakeData(), lambda: doc, now=0.0, stamp={})
    got = json.loads(ss.line())["body"]["tune"]
    assert got["lines"] == 7 and got["bad"] == 1 and got["errors"] == 2
    assert got["last_error"] == long_err[:hb_slim.HB_UDP_STR_MAX]
    assert "tune" in serial_status.SERIAL_BODY_EXTRA


finish()
