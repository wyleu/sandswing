# sand_status.py
#
# sand.status/v1 document builder.
# Safe on CPython and CircuitPython (no typing, no dataclasses).
#
# Device firmware collects a flat facts dict; this module turns it
# into the envelope the farm page already renders.

SCHEMA = "sand.status/v1"
LEVELS = ("ok", "warn", "error", "unknown")


def _level(value):
    v = (value or "unknown")
    if not isinstance(v, str):
        v = "unknown"
    v = v.lower()
    if v in LEVELS:
        return v
    return "unknown"


def _css_hex(value):
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return "#%06x" % (value & 0xFFFFFF)
    s = str(value).strip()
    if s.startswith("0x") or s.startswith("0X"):
        try:
            return "#%06x" % (int(s, 16) & 0xFFFFFF)
        except ValueError:
            return None
    if s.startswith("#") and len(s) >= 7:
        return s[:7]
    return None


def item(key, label, value, level=None, hint=None, color=None, color_alt=None):
    row = {
        "key": key,
        "label": label,
        "value": "—" if value is None or value == "" else str(value),
    }
    if level:
        row["level"] = _level(level)
    if hint:
        row["hint"] = str(hint)
    css = _css_hex(color)
    if css:
        row["color"] = css
    alt = _css_hex(color_alt)
    if alt:
        row["color_alt"] = alt
    return row


def section(sid, title, items, level=None):
    sec = {"id": sid, "title": title, "items": list(items)}
    if level:
        sec["level"] = _level(level)
    return sec


def worst_level(*levels):
    rank = {"ok": 0, "unknown": 1, "warn": 2, "error": 3}
    best = "ok"
    for lv in levels:
        r = rank.get(_level(lv), 1)
        if r > rank[best]:
            best = _level(lv)
    return best


def document(
    device_id,
    name,
    sections,
    role="other",
    family="",
    host="",
    instance="",
    ts=0,
    uptime_s=None,
    level="unknown",
    summary="",
    poll_hint_sec=5,
):
    doc = {
        "schema": SCHEMA,
        "id": device_id or name or "device",
        "name": name or device_id or "device",
        "role": role or "other",
        "family": family or "",
        "host": host or "",
        "instance": instance or "",
        "ts": int(ts) if ts else 0,
        "level": _level(level),
        "summary": str(summary or "")[:100],
        "poll_hint_sec": int(poll_hint_sec or 5),
        "sections": list(sections),
    }
    if uptime_s is not None:
        doc["uptime_s"] = int(uptime_s)
    return doc


def is_v1(data):
    return isinstance(data, dict) and data.get("schema") == SCHEMA


def from_sandsense(facts):
    f = facts or {}
    pll_on = bool(f.get("pll_enabled"))
    pll_locked = bool(f.get("pll_locked"))
    pll_level = "ok" if pll_locked else ("warn" if pll_on else "unknown")

    ip = f.get("ip")
    net_level = "error" if not ip else "ok"

    led = "off"
    led_color = "#111111"
    led_alt = None
    red_hex = f.get("tick_led_red_hex")
    green_hex = f.get("tick_led_green_hex")
    if f.get("tick_led_enabled"):
        if f.get("tick_led_red"):
            led = "red"
            led_color = red_hex
            led_alt = green_hex
        else:
            led = "green"
            led_color = green_hex
            led_alt = red_hex
    else:
        led_color = "#111111"

    adc = f.get("adc_avg")
    try:
        adc_s = int(adc) if adc is not None else None
    except (TypeError, ValueError):
        adc_s = adc

    temp = f.get("temp_c")
    temp_s = None
    if temp is not None:
        try:
            temp_s = "%.2f C" % float(temp)
        except (TypeError, ValueError):
            temp_s = str(temp)

    ticks = f.get("tick_count")
    interval = f.get("report_interval_s")
    tick_s = None
    if ticks is not None or interval is not None:
        tick_s = "%s / %ss" % (
            "—" if ticks is None else ticks,
            "—" if interval is None else int(interval),
        )

    level = worst_level(pll_level, net_level)
    summary = f.get("last_line") or (
        "PLL locked" if pll_locked else ("sensing" if ip else "no network")
    )

    sections = [
        section(
            "network",
            "Network",
            [
                item("ip", "IP", ip, net_level),
                item("ssid", "WiFi", f.get("ssid")),
            ],
            net_level,
        ),
        section(
            "sense",
            "Sensing",
            [
                item("mode", "Mode", f.get("mode") or "escapement"),
                item("pll", "PLL", "ON" if pll_on else "OFF", pll_level),
                item("lock", "Lock", "yes" if pll_locked else "no", pll_level),
                item("misses", "Misses", f.get("misses")),
                item("ticks", "Ticks", tick_s),
                item("tpm", "Last TPM", f.get("last_tpm")),
                item("seq", "Tick seq", f.get("tick_seq")),
                item("adc", "ADC avg", adc_s),
                item("leds", "Encoder LEDs", led, color=led_color, color_alt=led_alt),
                item("last", "Last message", f.get("last_line")),
            ],
            pll_level,
        ),
        section(
            "hardware",
            "Hardware",
            [
                item("temp", "Temp", temp_s),
            ],
        ),
    ]

    return document(
        device_id=f.get("id") or f.get("name") or "sandsense",
        name=f.get("name") or "sandsense",
        sections=sections,
        role=f.get("role") or "sense",
        family=f.get("family") or "sandsense",
        host=ip or "",
        instance=f.get("location") or "",
        ts=f.get("ts") or 0,
        uptime_s=f.get("uptime_s"),
        level=level,
        summary=summary,
        poll_hint_sec=f.get("report_interval_s") or f.get("poll_hint_sec") or 5,
    )


def from_sandswing(facts):
    """
    Lineup / scan facts the firmware supplies (missing → "—"):

      id, name, family, role, location
      ip, ssid
      mode                lineup | jabber | scan
      channel             1-based channel under test
      laser_gp, sense_gp
      duty, duty_pct
      pt                  True/False last photodiode
      changes             edges this sweep
      pass_totals         last PASS list
      fitted              list of fitted channel indices
      neo_on              bool
      neo_phase           bool  (False=green COLOR_A, True=red COLOR_B)
      last_line
      ts
    """
    f = facts or {}
    ip = f.get("ip")
    net_level = "error" if not ip else "ok"

    pt = f.get("pt")
    if pt is True:
        sense_level = "ok"
        pt_s = "True"
    elif pt is False:
        sense_level = "warn"
        pt_s = "False"
    else:
        sense_level = "unknown"
        pt_s = None

    neo_on = bool(f.get("neo_on"))
    phase = bool(f.get("neo_phase"))
    if neo_on:
        led = "red" if phase else "green"
        led_color = "#400000" if phase else "#004000"
        led_alt = "#004000" if phase else "#400000"
    else:
        led = "off"
        led_color = "#111111"
        led_alt = None

    ch = f.get("channel")
    fitted = f.get("fitted")
    if isinstance(fitted, (list, tuple)):
        fitted_s = ",".join(str(int(x) + 1) for x in fitted)
    else:
        fitted_s = fitted

    duty = f.get("duty")
    duty_pct = f.get("duty_pct")
    duty_s = None
    if duty is not None or duty_pct is not None:
        duty_s = "%s (%s%%)" % (
            "—" if duty is None else duty,
            "—" if duty_pct is None else int(duty_pct),
        )

    summary = f.get("last_line") or (
        "ch %s PT=%s" % (ch if ch is not None else "—", pt_s or "—")
    )
    level = worst_level(net_level, sense_level)

    sections = [
        section(
            "network",
            "Network",
            [
                item("ip", "IP", ip, net_level),
                item("ssid", "WiFi", f.get("ssid")),
            ],
            net_level,
        ),
        section(
            "sense",
            "Sensing",
            [
                item("mode", "Mode", f.get("mode") or "lineup"),
                item("channel", "Channel", ch),
                item("laser", "Laser GP", f.get("laser_gp")),
                item("sense", "Sense GP", f.get("sense_gp")),
                item("duty", "Duty", duty_s),
                item("pt", "PT", pt_s, sense_level),
                item("changes", "Edges", f.get("changes")),
                item("pass", "Last PASS", f.get("pass_totals")),
                item("fitted", "Fitted ch", fitted_s),
                item("leds", "NeoPixel", led, color=led_color, color_alt=led_alt),
                item("last", "Last message", f.get("last_line")),
            ],
            sense_level,
        ),
        section(
            "hardware",
            "Hardware",
            [
                item("neo", "NeoPixel", "on" if neo_on else "off"),
            ],
        ),
    ]

    return document(
        device_id=f.get("id") or f.get("name") or "sandswing",
        name=f.get("name") or "sandswing",
        sections=sections,
        role=f.get("role") or "swing",
        family=f.get("family") or "sandswing",
        host=ip or "",
        instance=f.get("location") or "",
        ts=f.get("ts") or 0,
        uptime_s=f.get("uptime_s"),
        level=level,
        summary=summary,
        poll_hint_sec=f.get("poll_hint_sec") or 2,
    )
def _ago(s):
    """Seconds -> '12 s' / '5 min' / '3 h 10 min' (None -> None)."""
    if s is None:
        return None
    s = int(s)
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % (s // 60)
    return "%d h %d min" % (s // 3600, (s % 3600) // 60)


def _signal(rssi):
    """dBm -> (text, level)."""
    if rssi is None:
        return None, "unknown"
    if rssi >= -67:
        return "%d dBm good" % rssi, "ok"
    if rssi >= -75:
        return "%d dBm fair" % rssi, "warn"
    return "%d dBm weak" % rssi, "error"


def _gateway_item(link):
    """Ping round trip to the Wi-Fi gateway: the link-quality stand-in when
    the radio cannot report signal strength."""
    ms, fail = link.get("gw_ms"), link.get("gw_fail") or 0
    if fail:
        return item("gateway", "Gateway ping", "no reply (%d missed)" % fail, "error")
    if ms is None:
        return item("gateway", "Gateway ping", None)
    lvl = "ok" if ms <= 50 else ("warn" if ms <= 200 else "error")
    return item("gateway", "Gateway ping", "%g ms" % ms, lvl)


def _time_items(t):
    st = t.get("state") or "unsynced"
    lvl = {"synced": "ok", "held": "ok", "stale": "warn"}.get(st, "error")
    peer = t.get("peer") or {}
    srv = t.get("srv") or peer.get("srv")
    stratum = t.get("stratum")
    if stratum is None:
        stratum = peer.get("stratum")
    src = None if srv is None else "%s stratum %s" % (srv, stratum)
    items = [item("sync", "Sync", st, lvl), item("source", "Source", src)]
    if t.get("age_s") is not None:
        a = t["age_s"]
        items.append(item("age", "Last good sync", _ago(a) + " ago",
                          "warn" if a > 600 else "ok"))
    last = t.get("last") or {}
    if last.get("ok") is False and last.get("why"):
        items.append(item("why", "Last attempt", last["why"], "warn"))
    if peer.get("leap") == 3:
        items.append(item("leap", "Server", "not synchronised (leap 3)", "warn"))
    return items, lvl


def _head_items(heads, chans):
    items = []
    for i, kind in enumerate(heads):
        c = chans[i] if i < len(chans) else {}
        v = str(kind)
        if c.get("n") is not None and kind in ("new", "old", "lb", "trigger"):
            v += " \u00b7 %d stroke%s" % (c["n"], "" if c["n"] == 1 else "s")
            if c.get("ago_s") is not None:
                v += ", last %s ago" % _ago(c["ago_s"])
        lvl = "error" if kind == "fault" else ("ok" if kind != "unknown" else "warn")
        items.append(item("ch%d" % (i + 1), "Channel %d" % (i + 1), v, lvl,
                          hint=c.get("why")))
        if kind == "fault" and c.get("why"):
            items.append(item("ch%d_why" % (i + 1), "  why", c["why"], "error"))
    return items


def from_concentrator(facts):
    f = facts or {}
    m = f.get("more") or {}
    ip = f.get("ip")
    net_level = "error" if not ip else "ok"
    heads = f.get("heads") or []
    tower = f.get("tower") or "unknown"
    tower_level = "ok" if tower == "active" else ("warn" if tower == "ringing_down" else "unknown")
    link = m.get("link") or {}

    net = [item("ip", "IP", ip, net_level),
           item("ssid", "WiFi", f.get("ssid"))]
    sig, sig_lvl = _signal(m.get("rssi"))
    sections = []
    if m:
        if m.get("rssi") is not None:
            net.append(item("rssi", "Signal", sig, sig_lvl))
        else:                                   # this build cannot report dBm
            net.append(_gateway_item(link))
        lost = link.get("wifi_lost") or 0
        net.append(item("wifi_lost", "Wi-Fi lost", "%d time%s" % (lost, "" if lost == 1 else "s"),
                        "warn" if lost else "ok",
                        hint="rejoins %s, radio resets %s" % (link.get("rejoins"), link.get("radio_resets"))))
        net.append(item("clients", "Sandbells clients", "%s connected, %s heard" % (
            link.get("clients", 0), link.get("live", 0)),
            "ok" if link.get("live") else "warn"))
        net.append(item("ws_drops", "Socket drops", link.get("drops", 0),
                        "warn" if link.get("drops") else "ok"))
        if link.get("last_drop_reason"):
            when = _ago(link.get("last_drop_age_s"))
            net.append(item("drop", "Last drop", "%s%s" % (
                link["last_drop_reason"], "" if when is None else ", " + when + " ago"), "warn"))
    sections.append(section("network", "Network", net,
                            worst_level(net_level, sig_lvl) if m else net_level))
    if m:
        titems, tlvl = _time_items(m.get("time") or {})
        sections.append(section("time", "Time", titems, tlvl))
    sections.append(section("heads", "Heads",
                            _head_items(heads, m.get("chans") or []) if m else
                            [item("ch%d" % (i + 1), "Channel %d" % (i + 1), k)
                             for i, k in enumerate(heads)], "ok"))
    sections.append(section("tower", "Tower", [
        item("state", "State", tower, tower_level),
    ], tower_level))
    if m:
        up = _ago(m.get("uptime_s"))
        reset = m.get("last_reset")
        conc = [item("uptime", "Up", up),
                item("reset", "Last reset", reset,
                     "warn" if reset and str(reset).upper() == "WATCHDOG" else None),
                item("watchdog", "Watchdog", "armed" if m.get("watchdog") else "off"),
                item("build", "Build", m.get("build") or "unknown")]
        if m.get("mem_free") is not None:
            conc.append(item("mem", "Free memory", "%d kB" % (m["mem_free"] // 1024)))
        conc.append(item("boot", "Boot / event", "%s / %s" % (m.get("boot_id"), m.get("seq"))))
        sections.append(section("concentrator", "Concentrator", conc))
    return document(
        device_id=f.get("id") or "concentrator",
        name=f.get("name") or "concentrator",
        sections=sections,
        role=f.get("role") or "concentrator",
        family=f.get("family") or "sandswing",
        host=ip or "",
        instance=f.get("location") or "",
        ts=f.get("ts") or 0,
        uptime_s=m.get("uptime_s"),
        level=net_level,
        # the Pico's latest log line (what mpremote would show last); the
        # head kinds are on the Heads panel
        summary=m.get("last_line") or "heads " + " ".join(str(h) for h in heads),
        poll_hint_sec=f.get("poll_hint_sec") or 2,
    )
