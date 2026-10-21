# time_servers.py -- which configured time servers to try from here. Leaf.
#
# time.ntp_ips lists every site's GPS box (home sandgps3 192.168.0.221,
# tower sandmon 10.42.0.1).  Only the ones on our own subnet are reachable,
# so trying the others just burns a socket timeout.  IPs only (no mDNS on
# the Pico); pure integer math on dotted quads.

OCTETS = 4
OCTET_MAX = 255
OCTET_BITS = 8


def normalise(ntp_ips):
    """A string (one IP, or several separated by commas/spaces) or a list
    -> list of IP strings.  A bare string is never split into characters."""
    if ntp_ips is None:
        return []
    if isinstance(ntp_ips, str):
        ntp_ips = ntp_ips.replace(",", " ").split()
    return [str(x).strip() for x in ntp_ips if str(x).strip()]


def ip_to_int(ip):
    """'a.b.c.d' -> 32-bit int, or None if not a dotted quad."""
    parts = str(ip).strip().split(".")
    if len(parts) != OCTETS:
        return None
    n = 0
    for p in parts:
        if not p.isdigit() or int(p) > OCTET_MAX:
            return None
        n = (n << OCTET_BITS) | int(p)
    return n


def same_subnet(ip, own_ip, netmask):
    a, b, m = ip_to_int(ip), ip_to_int(own_ip), ip_to_int(netmask)
    if a is None or b is None or m is None:
        return False
    return (a & m) == (b & m)


def candidates(ntp_ips, own_ip=None, netmask=None, log=print):
    """Servers worth querying from own_ip/netmask.  If none of them is on
    our subnet (or our address is unknown), fall back to the full list."""
    ips = normalise(ntp_ips)
    if own_ip is None or netmask is None:
        return ips
    near = [ip for ip in ips if same_subnet(ip, own_ip, netmask)]
    if not near and ips:
        log("time servers: none on %s/%s, trying all %s" % (own_ip, netmask, ips))
        return ips
    return near
