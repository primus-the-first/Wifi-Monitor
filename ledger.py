"""
The shared record: what each laptop reports, merged into one set of totals.

Laptops send *increments* ("device X downloaded N more bytes on day D"), never totals, so
adding them up is safe even when both laptops host at the same moment. Every batch has an id
and is applied at most once, so a retry after a dropped connection can't count anything twice.
Settings (cap, which networks are capped, device names) are last-writer-wins.
"""
import json
import os
import random
import re
from datetime import date, datetime, timedelta, timezone

DEFAULT_CAP_MB = 1200
ACTIVE_SECONDS = 90          # an agent seen this recently is "hosting now"
MAX_DELTA = 10**12           # sanity limit for one increment, in bytes
B310S_GATEWAY = "c8-14-51-e9-95-ad"

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAC_RE = re.compile(r"^([0-9a-f]{2}-){5}[0-9a-f]{2}$")


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")


def _is_day(x):
    return isinstance(x, str) and bool(DAY_RE.match(x))


def _is_mac(x):
    return isinstance(x, str) and bool(MAC_RE.match(x))


def _is_net(x):
    return x == "" or _is_mac(x)


def enforced_by_default():
    raw = os.environ.get("ENFORCED_GATEWAYS", B310S_GATEWAY)
    return {g.strip().lower() for g in raw.split(",") if g.strip()}


def init(db):
    with db.tx() as tx:
        for gateway in enforced_by_default():
            tx.execute("INSERT INTO networks (gateway, name, enforce) VALUES (?, ?, 1) "
                       "ON CONFLICT (gateway) DO NOTHING", (gateway, "Huawei B310s" if gateway == B310S_GATEWAY else ""))


# ---- applying what the laptops report -------------------------------------------------------

def apply_op(tx, op):
    """Applies one reported change. Returns False (and changes nothing) if it is malformed."""
    kind = op.get("k")
    if kind in ("usage", "reset"):
        day, net, mac = op.get("day"), op.get("network", ""), op.get("mac")
        if not (_is_day(day) and _is_net(net) and _is_mac(mac)):
            return False
        if kind == "usage":
            n = op.get("bytes")
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_DELTA:
                return False
            tx.execute("INSERT INTO usage (day, network, mac, bytes) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (day, network, mac) DO UPDATE SET bytes = usage.bytes + excluded.bytes",
                       (day, net, mac, n))
        else:
            tx.execute("INSERT INTO usage (day, network, mac, bytes) VALUES (?, ?, ?, 0) "
                       "ON CONFLICT (day, network, mac) DO UPDATE SET bytes = 0", (day, net, mac))
    elif kind == "rename":
        mac = op.get("mac")
        if not _is_mac(mac):
            return False
        tx.execute("INSERT INTO devices (mac, name) VALUES (?, ?) "
                   "ON CONFLICT (mac) DO UPDATE SET name = excluded.name", (mac, str(op.get("name", "")).strip()[:40]))
    elif kind == "block":  # a manual block: the device gets no internet until it is unblocked
        mac = op.get("mac")
        if not _is_mac(mac):
            return False
        if op.get("blocked"):
            tx.execute("INSERT INTO blocks (mac, since) VALUES (?, ?) ON CONFLICT (mac) DO NOTHING", (mac, utcnow()))
        else:
            tx.execute("DELETE FROM blocks WHERE mac = ?", (mac,))
    elif kind == "enforce":
        gateway = op.get("gateway")
        if not _is_mac(gateway):
            return False
        tx.execute("INSERT INTO networks (gateway, name, enforce) VALUES (?, '', ?) "
                   "ON CONFLICT (gateway) DO UPDATE SET enforce = excluded.enforce", (gateway, int(bool(op.get("enforce")))))
    elif kind == "cap":
        try:
            mb = float(op.get("mb"))
        except (TypeError, ValueError):
            return False
        if not 1 <= mb <= 1_000_000:
            return False
        tx.execute("INSERT INTO settings (key, value) VALUES ('cap_mb', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(mb),))
    else:
        return False
    return True


def _clean_presence(raw):
    """Keeps only the fields we use, with sane sizes."""
    raw = raw if isinstance(raw, dict) else {}
    net = raw.get("network") if isinstance(raw.get("network"), dict) else {}
    clients = []
    for c in (raw.get("clients") or [])[:40]:
        if isinstance(c, dict) and _is_mac(c.get("mac")):
            clients.append({"mac": c["mac"], "ip": str(c.get("ip") or "")[:20], "hostname": str(c.get("hostname") or "")[:40]})
    return {
        "network": {"gateway": net.get("gateway") if _is_mac(net.get("gateway")) else "", "name": str(net.get("name") or "")[:60]},
        "clients": clients,
        "enforce": bool(raw.get("enforce")),
        "free": bool(raw.get("free")),
        "day": raw.get("day") if _is_day(raw.get("day")) else "",
        "blocked": [m for m in (raw.get("blocked") or [])[:40] if _is_mac(m)],
    }


def _note_presence(tx, agent, presence, now):
    tx.execute("INSERT INTO agents (name, last_seen, presence) VALUES (?, ?, ?) "
               "ON CONFLICT (name) DO UPDATE SET last_seen = excluded.last_seen, presence = excluded.presence",
               (agent, now, json.dumps(presence)))
    gateway = presence["network"]["gateway"]
    if gateway:
        tx.execute("INSERT INTO networks (gateway, name, enforce, last_seen) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (gateway) DO UPDATE SET last_seen = excluded.last_seen, "
                   "name = CASE WHEN networks.name = '' THEN excluded.name ELSE networks.name END",
                   (gateway, presence["network"]["name"], int(gateway in enforced_by_default()), now))
    for c in presence["clients"]:
        tx.execute("INSERT INTO devices (mac, name, last_ip, last_seen) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (mac) DO UPDATE SET last_ip = excluded.last_ip, last_seen = excluded.last_seen, "
                   "name = CASE WHEN devices.name = '' THEN excluded.name ELSE devices.name END",
                   (c["mac"], c["hostname"], c["ip"], now))


def server_state(tx, since):
    """The shared settings and usage since a day, for a laptop to copy locally."""
    row = tx.execute("SELECT value FROM settings WHERE key = 'cap_mb'")
    return {
        "cap_mb": float(row[0]["value"]) if row else DEFAULT_CAP_MB,
        "networks": {r["gateway"]: {"name": r["name"], "enforce": bool(r["enforce"])}
                     for r in tx.execute("SELECT gateway, name, enforce FROM networks")},
        "devices": {r["mac"]: r["name"] for r in tx.execute("SELECT mac, name FROM devices")},
        "blocked": [r["mac"] for r in tx.execute("SELECT mac FROM blocks")],
        "usage":[{"day": r["day"], "network": r["network"], "mac": r["mac"], "bytes": int(r["bytes"])}
                  for r in tx.execute("SELECT day, network, mac, bytes FROM usage WHERE day >= ?", (since,))],
    }


def _active_agents(tx, now_minus=ACTIVE_SECONDS):
    rows = tx.execute("SELECT name, last_seen, presence FROM agents WHERE last_seen >= ?", (_ago(now_minus),))
    return [(r["name"], r["last_seen"], json.loads(r["presence"])) for r in rows]


def sync(db, payload):
    """One round trip from a laptop: apply its reported changes, return the current shared state."""
    now = utcnow()
    agent = str(payload.get("agent") or "").strip()[:40] or "unknown"
    since = payload.get("since") if _is_day(payload.get("since")) else "0000-00-00"
    applied = rejected = 0
    with db.tx() as tx:
        for batch in list(payload.get("batches") or [])[:100]:
            batch_id, ops = str(batch.get("id") or "")[:64], batch.get("ops")
            if not batch_id or not isinstance(ops, list):
                continue
            tx.execute("INSERT INTO batches (id, received) VALUES (?, ?) ON CONFLICT (id) DO NOTHING", (batch_id, now))
            if tx.rowcount == 0:
                continue  # already applied: this is a retry of a batch whose reply was lost
            for op in ops[:50000]:
                if isinstance(op, dict) and apply_op(tx, op):
                    applied += 1
                else:
                    rejected += 1
        _note_presence(tx, agent, _clean_presence(payload.get("presence")), now)
        state = server_state(tx, since)
        others = [{"name": n, "gateway": p["network"]["gateway"], "network": p["network"]["name"],
                   "devices": len(p["clients"])} for n, _, p in _active_agents(tx) if n != agent]
        if random.random() < 0.02:  # now and then, forget old batch ids
            tx.execute("DELETE FROM batches WHERE received < ?", (_ago(14 * 86400),))
    return {"ok": True, "applied": applied, "rejected": rejected, "state": state, "others": others}


# ---- the admin page ------------------------------------------------------------------------

def admin_view(db, network=None):
    now = utcnow()
    with db.tx() as tx:
        agents = _active_agents(tx, 86400 * 30)
        cap_rows = tx.execute("SELECT value FROM settings WHERE key = 'cap_mb'")
        networks = tx.execute("SELECT gateway, name, enforce, last_seen FROM networks ORDER BY last_seen DESC, name")
        hosting = {}  # network -> agents active there now
        for name, seen, p in agents:
            if seen >= _ago(ACTIVE_SECONDS) and p["network"]["gateway"]:
                hosting.setdefault(p["network"]["gateway"], []).append((name, p))
        known = [n["gateway"] for n in networks]
        view = network if network in known else (next(iter(hosting), None) or (known[0] if known else ""))
        newest = max(agents, key=lambda a: a[1], default=None)
        day = (newest[2]["day"] if newest else "") or ""
        if not day:
            rows = tx.execute("SELECT MAX(day) AS d FROM usage")
            day = (rows[0]["d"] if rows and rows[0]["d"] else date.today().isoformat())
        week_start = (date.fromisoformat(day) - timedelta(days=6)).isoformat()
        usage = tx.execute(
            "SELECT mac, CAST(SUM(bytes) AS BIGINT) AS total, "
            "CAST(SUM(CASE WHEN day >= ? THEN bytes ELSE 0 END) AS BIGINT) AS week, "
            "CAST(SUM(CASE WHEN day = ? THEN bytes ELSE 0 END) AS BIGINT) AS today, "
            "MAX(CASE WHEN bytes > 0 THEN day END) AS last_day "
            "FROM usage WHERE network = ? GROUP BY mac", (week_start, day, view))
        history = tx.execute("SELECT day, CAST(SUM(bytes) AS BIGINT) AS total FROM usage "
                             "WHERE network = ? AND day >= ? GROUP BY day ORDER BY day", (view, week_start))
        names = {r["mac"]: r["name"] for r in tx.execute("SELECT mac, name FROM devices")}
        manual = {r["mac"] for r in tx.execute("SELECT mac FROM blocks")}
    here = hosting.get(view, [])
    online = {c["mac"]: c["ip"] for _, p in here for c in p["clients"]}
    blocked = {m for _, p in here for m in p["blocked"]}
    devices = []
    for mac in set(u["mac"] for u in usage) | set(online) | manual:
        u = next((x for x in usage if x["mac"] == mac), {"today": 0, "week": 0, "total": 0, "last_day": None})
        devices.append({"mac": mac, "name": names.get(mac, ""), "ip": online.get(mac, ""), "online": mac in online,
                        "manual": mac in manual, "blocked": mac in manual or mac in blocked,
                        "used": int(u["today"]), "week": int(u["week"]),
                        "total": int(u["total"]), "last_day": u["last_day"]})
    devices.sort(key=lambda d: (not d["online"], -d["used"], d["name"].lower()))
    seen_now = _ago(ACTIVE_SECONDS)
    return {
        "day": day,
        "cap": int(float(cap_rows[0]["value"] if cap_rows else DEFAULT_CAP_MB) * 1_000_000),
        "view": view,
        "networks": [{"gateway": n["gateway"], "name": n["name"] or n["gateway"], "enforce": bool(n["enforce"]),
                      "hosted": n["gateway"] in hosting} for n in networks],
        "devices": devices,
        "history": [{"day": h["day"], "total": int(h["total"])} for h in history],
        "agents": [{"name": n, "active": s >= seen_now, "last_seen": s, "network": p["network"]["name"],
                    "devices": len(p["clients"]), "blocked": len(p["blocked"]), "free": p["free"],
                    "enforce": p["enforce"]} for n, s, p in sorted(agents, key=lambda a: a[1], reverse=True)],
        "server_time": now,
    }


def admin_op(db, kind, data):
    """An admin change from the web page, applied exactly like one reported by a laptop."""
    ops = {"cap": lambda: {"k": "cap", "mb": data.get("cap_mb")},
           "enforce": lambda: {"k": "enforce", "gateway": data.get("gateway"), "enforce": data.get("enforce")},
           "rename": lambda: {"k": "rename", "mac": data.get("mac"), "name": data.get("name", "")},
           "block": lambda: {"k": "block", "mac": data.get("mac"), "blocked": data.get("blocked")},
           "reset": lambda: {"k": "reset", "day": data.get("day"), "network": data.get("network", ""), "mac": data.get("mac")}}
    if kind not in ops:
        return False
    with db.tx() as tx:
        return apply_op(tx, ops[kind]())
