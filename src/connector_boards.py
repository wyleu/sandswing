# connector_boards.py -- which channels have a connector board. Leaf.
# ch 1-4 on board 1 (fitted); ch 5-8 are on the Pico board but run to an
# expansion connector for a future board 2.  present: True / False / None
# (None = auto: all channels 'empty' -> taken as absent).

DEFAULT_BOARDS = [
    {"name": "conn1", "channels": [0, 1, 2, 3], "present": True},
    {"name": "conn2", "channels": [4, 5, 6, 7], "present": False},
]
POPULATED = [c for b in DEFAULT_BOARDS if b.get("present") for c in b["channels"]]


def board_of(ch, boards=DEFAULT_BOARDS):
    for b in boards:
        if ch in b["channels"]:
            return b
    return None


def apply_boards(results, boards=DEFAULT_BOARDS):
    """results: {ch: kind}.  Channels on an absent board become 'no_board'."""
    out = dict(results)
    for b in boards:
        chans = [c for c in b["channels"] if c in out]
        if b.get("present") is False:
            for c in chans:
                out[c] = "no_board"
        elif b.get("present") is None and chans and \
                all(out[c] == "empty" for c in chans):
            for c in chans:
                out[c] = "no_board"
    return out
