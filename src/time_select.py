# time_select.py -- pick one time server from a probe of each. Leaf.
#
# probe result per server: {"ip", "stratum", "rtt_ms", "offset_ms"}, with
# offset_ms None when it did not answer.  Lowest valid stratum wins, then
# the lowest RTT.  Two valid servers whose offsets differ by more than
# SERVER_DISAGREE_MS are flagged (one of the GPS boxes is wrong).

STRATUM_MIN = 1                 # 0 = kiss-o'-death / unspecified
STRATUM_MAX = 15                # 16 = unsynchronised
SERVER_DISAGREE_MS = 20         # GPS boxes agree to ~1 ms; Wi-Fi adds a few


def valid(r):
    return (r.get("offset_ms") is not None and r.get("stratum") is not None
            and STRATUM_MIN <= r["stratum"] <= STRATUM_MAX)


def choose(results):
    """-> (best result or None, disagree flag)."""
    ok = [r for r in results if valid(r)]
    if not ok:
        return None, False
    best = min(ok, key=lambda r: (r["stratum"], r["rtt_ms"]))
    offs = [r["offset_ms"] for r in ok]
    return best, (max(offs) - min(offs)) > SERVER_DISAGREE_MS
