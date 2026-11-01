# heap_watch.py -- how big the VM (gc) heap is, and when it grows.
#
# Why (2026-11-01 08:18-08:21, docs/link-health.md "Where lwIP's memory goes"):
# on the Pico 2 W, lwIP has no pool of its own.  lwipopts.h sets
# MEM_CUSTOM_ALLOCATOR (lwip_mem.c -> port_malloc) and MEMP_MEM_MALLOC 1, so
# every pbuf and TCP segment comes from CircuitPython's port heap (TLSF).
# The VM heap lives in the SAME port heap and grows on demand
# (MICROPY_GC_SPLIT_HEAP_AUTO, py/gc.c gc_try_add_heap): it doubles, and when
# doubling no longer fits it takes the LARGEST free block there is.  After
# that, lwIP cannot get one 1.5 kB pbuf (LWIP_NETIF_TX_SINGLE_PBUF: every
# segment is a whole MSS) and every socket.send() sleeps 10 s in C.
# gc.mem_free() alone cannot show it (92 kB free inside the heap while the
# RAM outside it is gone); the heap's total size can: free + alloc.
import gc

KB = 1024
_state = {"total": None, "start": None, "max": 0, "grew": 0, "last_free": None}


def sample():
    """(total, free) bytes of the VM heap, all areas.  (0, 0) where gc has
    no mem_free / mem_alloc (CPython: the host tests give fakes)."""
    mem_free = getattr(gc, "mem_free", None)
    mem_alloc = getattr(gc, "mem_alloc", None)
    if mem_free is None or mem_alloc is None:
        return 0, 0
    free = mem_free()
    return free + mem_alloc(), free


def check(log=None, where=""):
    """Sample; log once each time the heap has grown since the last sample
    (the RAM it took is RAM lwIP can no longer have).  Returns (total, free)."""
    total, free = sample()
    prev = _state["total"]
    if prev is None:
        _state["start"] = total
    elif total > prev:
        _state["grew"] += 1
        if log is not None:
            log("HEAP GREW %d -> %d kB%s (%d kB free in it): the VM heap took RAM that"
                " lwIP allocates its packet buffers from; network sends may now"
                " stall 10 s (ERR_MEM); grew %d time(s) this boot"
                % (prev // KB, total // KB, (" at " + where) if where else "",
                   free // KB, _state["grew"]))
    _state["total"] = total
    _state["max"] = max(_state["max"], total)
    _state["last_free"] = free
    return total, free


def start_setting():
    """CIRCUITPY_HEAP_START_SIZE from settings.toml (None: the build's 8 kB)."""
    try:
        import os
        v = os.getenv("CIRCUITPY_HEAP_START_SIZE")
    except Exception:
        return None
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def status():
    """For /status and the heartbeat."""
    t, m = _state["total"], _state["max"]
    return {"gc_heap_kb": None if t is None else t // KB,
            "gc_heap_max_kb": m // KB,
            "gc_heap_boot_kb": None if _state["start"] is None else _state["start"] // KB,
            "gc_heap_grew": _state["grew"]}


def reset():
    """Tests."""
    _state.update(total=None, start=None, max=0, grew=0, last_free=None)
