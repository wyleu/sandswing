"""status_doc.py -- the /status document, built once per change and kept as
small pieces, so it is never one big allocation.

2026-10-29 15:42-15:47: "status payload error memory allocation failed,
allocating 6728 bytes" and "ws poll: memory allocation failed, allocating
7382 bytes" with 106-110 kB free: json.dumps() of the whole document, then
headers + body joined, each needed one contiguous ~7 kB block, and a
fragmented heap had none.  Worse, on CircuitPython 10 (MICROPY_GC_SPLIT_HEAP_AUTO)
a large block the Python heap cannot find makes it grow into the port heap
(tlsf, shared with lwIP's pbufs), which is what then starves lwIP (ERR_MEM).

Here every unit is serialised on its own (the head fields; each section by
id; each part by key), compared with what was sent before, and stamped with
the version at which it last changed.  A reply is a list of those pieces
plus a few separators; farm_ws writes them in chunks without joining them.

    doc = StatusDoc(boot_id)
    doc.refresh(now, payload_fn, min_s)  -> True if rebuilt
    doc.full(parts=True)                 -> [pieces]   whole document
    doc.delta(since)                     -> [pieces]   only what changed after `since`
    doc.knows(since, boot)               -> can a delta from `since` be given?
"""
import json

HEAD = "head"


def dumps(obj):
    """Compact JSON (no spaces) where the port allows it."""
    try:
        return json.dumps(obj, separators=(",", ":"))
    except TypeError:
        return json.dumps(obj)


def _b(s):
    return s.encode("utf-8") if isinstance(s, str) else s


def _inner(b):
    """'{...}' -> the bytes between the braces, without copying."""
    return memoryview(b)[1:len(b) - 1]


class StatusDoc:
    def __init__(self, boot_id):
        self.boot_id = str(boot_id)
        self.reset()

    def reset(self):
        """Forget everything (also: give the memory back after a MemoryError)."""
        self.version = 0
        self.units = {}          # key -> [version, bytes]; keys "head", "s:<id>", "p:<key>"
        self.order = []          # section ids, in document order
        self.parts = []          # part keys, in document order
        self.gone = {}           # key -> version at which it disappeared
        self.built_at = None
        self.size_b = 0          # the last full document's size (with parts)

    def refresh(self, now, payload_fn, min_s):
        """Rebuild from payload_fn() unless built less than min_s ago."""
        if self.built_at is not None and now - self.built_at < min_s:
            return False
        doc = payload_fn()
        self.built_at = now
        seen = {}
        order, parts = [], []
        head = {}
        for k in doc:
            if k == "sections":
                for sec in doc[k]:
                    sid = str(sec.get("id"))
                    order.append(sid)
                    seen["s:" + sid] = _b(dumps(sec))
            elif k == "parts":
                for pk in doc[k]:
                    parts.append(pk)
                    seen["p:" + pk] = _b(dumps(doc[k][pk]))
            else:
                head[k] = doc[k]
        seen[HEAD] = _b(dumps(head))
        doc = head = None
        changed = [k for k in seen
                   if k not in self.units or self.units[k][1] != seen[k]]
        removed = [k for k in self.units if k not in seen]
        if changed or removed or order != self.order:
            self.version += 1
            for k in changed:
                self.units[k] = [self.version, seen[k]]
                self.gone.pop(k, None)
            for k in removed:
                del self.units[k]
                self.gone[k] = self.version
        self.order, self.parts = order, parts
        self.size_b = sum(len(p) for p in self.full(parts=True))
        return True

    def knows(self, since, boot):
        """A delta needs the same boot and a version this document has had."""
        return (self.built_at is not None and boot == self.boot_id
                and since is not None and 0 < since <= self.version)

    def _stamp(self, full):
        return _b(',"status_version":%d,"boot_id":"%s","full":%s'
                  % (self.version, self.boot_id, "true" if full else "false"))

    def _sections(self, keys):
        out = [b',"sections":[']
        for i, sid in enumerate(keys):
            if i:
                out.append(b",")
            out.append(self.units["s:" + sid][1])
        out.append(b"]")
        return out

    def full(self, parts=True):
        """The whole document (as the old /status), as pieces."""
        out = [b"{", _inner(self.units[HEAD][1]), self._stamp(True)]
        out.extend(self._sections(self.order))
        if parts and self.parts:
            out.append(b',"parts":{')
            for i, pk in enumerate(self.parts):
                out.append(_b('%s"%s":' % ("," if i else "", pk)))
                out.append(self.units["p:" + pk][1])
            out.append(b"}")
        out.append(b"}")
        return out

    def delta(self, since):
        """What changed after version `since`: the head fields if they did,
        changed sections, the section order, removed sections.  No parts."""
        head = self.units[HEAD]
        out = [_b('{"schema":"sand.status/v1","delta":true,"since":%d' % since),
               self._stamp(False),
               _b(',"order":%s' % dumps(self.order))]
        if head[0] > since:
            out.append(b',"head":')
            out.append(head[1])
        out.extend(self._sections([s for s in self.order
                                   if self.units["s:" + s][0] > since]))
        gone = [k[2:] for k, v in self.gone.items() if v > since and k.startswith("s:")]
        if gone:
            out.append(_b(',"removed":%s' % dumps(gone)))
        out.append(b"}")
        return out


def size(pieces):
    return sum(len(p) for p in pieces)


def join(pieces):
    """Tests and tools only: never on the Pico's hot path."""
    return b"".join(bytes(p) for p in pieces)
