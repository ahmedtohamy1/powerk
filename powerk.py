#!/usr/bin/env python3
"""powerk - local control for the LG U+ / Jinheung MTTL-W01 4-outlet smart strip.

Stdlib only, one file, three commands:

  python powerk.py serve       TCP 10086 device server + web UI on 8080  (default)
  python powerk.py provision   point the strip at this PC (while in SoftAP setup mode)
  python powerk.py selftest    offline protocol + rules + energy check

The strip speaks plain text lines ("up:...") over TCP 10086. It connects to this
PC directly because provisioning stores the PC's IP in the strip. No cloud, no
router DNS or DNAT change, no MQTT, no Home Assistant.

The server also runs schedules, timers, cycles and auto-off rules, keeps an hourly
energy history (SQLite) with cost and a monthly budget, and sends alerts (ntfy /
Telegram). Names, rules and settings live in powerk.json, history in powerk.db.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

DEVICE_PORT = 10086
SETUP_HOST = "192.168.1.1"      # strip SoftAP setup service
SETUP_PORT = 30300
WEB_PORT = 8080
POLL_SECONDS = 5.0
DIAG_EVERY = 3                  # voltage/current/RSSI every 3rd poll
MAX_MISSES = 3                  # unanswered polls before a silent (half-open) session is dropped
OFFLINE_ALERT_AFTER = 60        # seconds offline before an "offline" alert (the strip re-dials often)
MAX_OUTLET_W = 4000             # energy counter jumps above this power between two readings are glitches
BUDGET_STEPS = (50, 75, 90, 100)
NAME_MAX = 40

DEFAULT_SETTINGS = {
    "currency": "EGP",
    "price_kwh": 2.14,
    "budget": None,             # monthly, in currency; None = off
    "temp_max_c": 60,
    "power_max_w": 3500,
    "ntfy_url": "",
    "telegram_token": "",
    "telegram_chat": "",
}

BOOTINFO_RE = re.compile(
    r"^up:bootinfo:([^;\r\n]+);([0-9A-Fa-f]{12});([0-9A-Fa-f]{12});([^;\r\n]+);connect$"
)
GETINFO_RE = re.compile(
    r"(?P<ch>[1-5]):(?P<runtime>-?\d+);(?P<relay>on|off);(?P<state>-?\d+);"
    r"(?P<overload>[^;:]+);(?P<overheat>[^;:]+);(?P<power>-?\d+);"
    r"(?P<energy>[0-9A-Fa-f]{8});(?P<previous>[0-9A-Fa-f]{8});"
    r"(?P<config>[0-9A-Fa-f]{8});(?P<status>[^;:]+);"
    r"(?P<event>[0-9A-Fa-f]{2});(?P<temperature>-?\d+)",
    re.I,
)
ONOFF_ACK_RE = re.compile(r"^up:onoff:([1-4]):(on|off)$", re.I)
EVENT_RE = re.compile(r"^up:event:onoff:([0-4]):(on|off)$", re.I)
POWER_REPORT_RE = re.compile(r"^up:power_report:([1-5]):(-?\d+)$", re.I)
QUERY_RE = re.compile(r"^up:query:(-?\d+)$")
MAC_RE = re.compile(r"^[0-9A-F]{12}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_getinfo(line: str) -> list[dict]:
    """Parse 'up:getinfo:...' into per-outlet records (channel 5 is the aggregate)."""
    text = line.strip("\r\n\x00 ")
    if text.startswith("up:getinfo:"):
        text = text[len("up:getinfo:"):]
    out = []
    for m in GETINFO_RE.finditer(text):
        g = m.groupdict()
        ch = int(g["ch"])
        if ch > 4:
            continue
        out.append({
            "n": ch,
            "on": g["relay"].lower() == "on",
            "power_w": round(int(g["power"]) / 1000.0, 2),
            "energy_kwh": round(int(g["energy"], 16) / 1000.0, 3),
            "temp_c": int(g["temperature"]),
            # overload/overheat read "on;on" on an idle, cool strip (fw 0.1.54-1.0.66): they look like
            # protection *settings*, not trips, so they are not reported as alarms.
        })
    return out


# ---------------------------------------------------------------- persistence

class Store:
    """powerk.json: names, rules, settings, small bookkeeping. Shared by web threads and the loop."""

    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"names": {}, "rules": [], "settings": dict(DEFAULT_SETTINGS), "meta": {}}
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError) as err:
                raise SystemExit(f"cannot read {path}: {err} (fix or move it away)")
            for key in ("names", "rules", "meta"):
                if isinstance(loaded.get(key), type(self.data[key])):
                    self.data[key] = loaded[key]
            if isinstance(loaded.get("settings"), dict):
                self.data["settings"].update({k: v for k, v in loaded["settings"].items() if k in DEFAULT_SETTINGS})

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            text = json.dumps(self.data, indent=1, ensure_ascii=False)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, self.path)

    # names: {"MAC": {"0": "strip label", "1": "Fridge", ...}}
    def name(self, mac: str, n: int) -> str:
        with self.lock:
            return self.data["names"].get(mac, {}).get(str(n), "")

    def set_name(self, mac: str, n: int, name: str) -> None:
        with self.lock:
            names = self.data["names"].setdefault(mac, {})
            if name:
                names[str(n)] = name
            else:
                names.pop(str(n), None)
            self.save()

    def settings(self) -> dict:
        with self.lock:
            return dict(self.data["settings"])

    def update_settings(self, changes: dict) -> dict:
        with self.lock:
            self.data["settings"].update(changes)
            self.save()
            return dict(self.data["settings"])

    def meta(self, key: str, default=None):
        with self.lock:
            return copy.deepcopy(self.data["meta"].get(key, default))

    def set_meta(self, key: str, value) -> None:
        with self.lock:
            self.data["meta"][key] = value
            self.save()

    def raise_meta(self, key: str, value: int) -> int:
        """Move a numeric cursor forward only (two clients marking read never move it back)."""
        with self.lock:
            value = max(value, self.data["meta"].get(key, 0))
            self.data["meta"][key] = value
            self.save()
            return value

    def rules(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.data["rules"])

    def rule(self, rule_id: str) -> dict | None:
        with self.lock:
            return next((copy.deepcopy(r) for r in self.data["rules"] if r["id"] == rule_id), None)

    def put_rule(self, rule: dict) -> None:
        with self.lock:
            rules = self.data["rules"]
            for i, r in enumerate(rules):
                if r["id"] == rule["id"]:
                    rules[i] = rule
                    break
            else:
                rules.append(rule)
            self.save()

    def consume_rule(self, rule: dict) -> bool:
        """Delete a rule only if it is unchanged since the caller read it (a timer edited meanwhile survives)."""
        with self.lock:
            if rule not in self.data["rules"]:
                return False
            self.data["rules"].remove(rule)
            self.save()
            return True

    def delete_rule(self, rule_id: str) -> bool:
        with self.lock:
            before = len(self.data["rules"])
            self.data["rules"] = [r for r in self.data["rules"] if r["id"] != rule_id]
            if len(self.data["rules"]) == before:
                return False
            self.save()
            return True


def hour_start(ts: float) -> int:
    return int(datetime.fromtimestamp(ts).replace(minute=0, second=0, microsecond=0).timestamp())


def day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(dt: datetime, back: int = 0) -> datetime:
    y, m = divmod(dt.year * 12 + dt.month - 1 - back, 12)
    return datetime(y, m + 1, 1)


class Db:
    """powerk.db: hourly energy per outlet + the event log."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        with self.lock, self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS energy(mac TEXT, outlet INTEGER, hour INTEGER, wh REAL,"
                              " PRIMARY KEY(mac, outlet, hour))")
            self.conn.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, code TEXT,"
                              " mac TEXT, outlet INTEGER, text TEXT, params TEXT)")
        self._totals: dict[str, tuple[float, dict]] = {}

    def add_energy(self, mac: str, outlet: int, wh: float, ts: float, since: float | None = None) -> None:
        """Add wh consumed up to ts. After a long gap (strip offline, counter kept running) the delta
        is spread evenly over the hours since the previous reading instead of landing in one hour."""
        hours = [hour_start(ts)]
        if since and ts - since > 3600:
            since = max(since, ts - 366 * 86400)
            hours = sorted({hour_start(t) for t in range(int(since), int(ts), 900)} | {hour_start(ts - 1)})
        share = wh / len(hours)
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO energy VALUES(?,?,?,?) ON CONFLICT(mac, outlet, hour)"
                                  " DO UPDATE SET wh = wh + excluded.wh", [(mac, outlet, h, share) for h in hours])
        self._totals.clear()

    def _sum_wh(self, mac: str | None, since: float) -> float:
        sql, args = "SELECT COALESCE(SUM(wh), 0) FROM energy WHERE hour >= ?", [int(since)]
        if mac:
            sql, args = sql + " AND mac = ?", args + [mac]
        with self.lock:
            return self.conn.execute(sql, args).fetchone()[0]

    def totals(self, mac: str | None = None) -> dict:
        """today/month kWh, cached a few seconds (every /api/state asks)."""
        key = mac or "*"
        hit = self._totals.get(key)
        if hit and time.monotonic() - hit[0] < 10:
            return hit[1]
        now = datetime.now()
        value = {
            "today_kwh": round(self._sum_wh(mac, day_start(now).timestamp()) / 1000.0, 3),
            "month_kwh": round(self._sum_wh(mac, month_start(now).timestamp()) / 1000.0, 3),
        }
        self._totals[key] = (time.monotonic(), value)
        return value

    def history(self, mac: str, rng: str, price: float, outlet: int | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        if rng == "day":                                   # 24 elapsed hours, stepped in epoch time (DST-safe)
            unit, last = "hour", hour_start(now.timestamp())
            starts = [datetime.fromtimestamp(last - (23 - i) * 3600) for i in range(24)]
        elif rng in ("week", "month"):
            days = 7 if rng == "week" else 30
            unit, first = "day", day_start(now) - timedelta(days=days - 1)
            starts = [day_start(first + timedelta(days=i, hours=3)) for i in range(days)]   # +3h: DST-safe day step
        elif rng == "year":
            unit = "month"
            starts = [month_start(now, back) for back in range(11, -1, -1)]
        else:
            raise ValueError("range must be day, week, month or year")

        def bucket(ts: int) -> int:
            if unit == "hour":
                return ts
            dt = datetime.fromtimestamp(ts)
            if unit == "day":
                dt = day_start(dt)
            elif unit == "month":
                dt = month_start(dt)
            return int(dt.timestamp())

        sql, args = "SELECT outlet, hour, wh FROM energy WHERE mac = ? AND hour >= ?", [mac, int(starts[0].timestamp())]
        if outlet:
            sql, args = sql + " AND outlet = ?", args + [outlet]
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        sums = {(last - (23 - i) * 3600 if unit == "hour" else int(s.timestamp())): 0.0 for i, s in enumerate(starts)}
        per_outlet = {n: 0.0 for n in (1, 2, 3, 4)}
        for n, hour, wh in rows:
            key = bucket(hour)
            if key in sums:
                sums[key] += wh
                per_outlet[n] = per_outlet.get(n, 0.0) + wh
        total = sum(sums.values()) / 1000.0
        return {
            "range": rng, "unit": unit,
            "buckets": [{"t": t, "kwh": round(wh / 1000.0, 3), "cost": round(wh / 1000.0 * price, 2)} for t, wh in sums.items()],
            "total_kwh": round(total, 3), "total_cost": round(total * price, 2),
            "per_outlet": [{"n": n, "kwh": round(wh / 1000.0, 3)} for n, wh in sorted(per_outlet.items())],
        }

    def add_event(self, ev: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO events(ts, level, code, mac, outlet, text, params) VALUES(?,?,?,?,?,?,?)",
                              (ev["ts"], ev["level"], ev["code"], ev["mac"], ev["outlet"], ev["text"], json.dumps(ev["params"])))
            self.conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 500")

    def events(self, limit: int = 50, before_id: int | None = None) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, ts, level, code, mac, outlet, text, params FROM events WHERE id < ?"
                                     " ORDER BY id DESC LIMIT ?", (before_id or 2**62, limit)).fetchall()
        return [{"id": r[0], "ts": int(r[1]), "level": r[2], "code": r[3], "mac": r[4], "outlet": r[5], "text": r[6],
                 "params": json.loads(r[7] or "{}")} for r in rows]

    def last_event_id(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def unread(self, read_through: int) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM events WHERE id > ? AND level != 'info'", (read_through,)).fetchone()[0]


# ---------------------------------------------------------------- alerts

NO_PUSH = {"rule_fired", "online"}


class Notifier:
    """Push to ntfy and/or Telegram from a background thread; failures are logged, never raised."""

    def __init__(self, store: Store):
        self.store = store

    def _jobs(self, title: str, text: str) -> list[tuple]:
        s = self.store.settings()
        jobs = []
        if s["ntfy_url"]:
            jobs.append(("ntfy", s["ntfy_url"], text.encode(), {"Title": title.encode("ascii", "replace").decode()}))
        if s["telegram_token"] and s["telegram_chat"]:
            jobs.append(("telegram", f"https://api.telegram.org/bot{s['telegram_token']}/sendMessage",
                         urlencode({"chat_id": s["telegram_chat"], "text": f"{title}\n{text}"}).encode(),
                         {"Content-Type": "application/x-www-form-urlencoded"}))
        return jobs

    def send(self, title: str, text: str) -> None:
        for job in self._jobs(title, text):
            threading.Thread(target=self._post, args=job[1:], daemon=True).start()

    def test(self) -> tuple[list[str], list[dict]]:
        sent, failed = [], []
        for name, *job in self._jobs("powerk: test", "Test notification from powerk"):
            error = self._post(*job)
            if error:
                failed.append({"channel": name, "error": error})
            else:
                sent.append(name)
        return sent, failed

    @staticmethod
    def _post(url: str, body: bytes, headers: dict) -> str | None:
        """None on success, else a short error (the URL is never echoed: it can hold the bot token)."""
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return None
                error = f"HTTP {resp.status}"
        except urllib.error.HTTPError as err:
            error = f"HTTP {err.code}"
        except Exception as err:
            error = type(err).__name__ + (f": {err.reason}" if hasattr(err, "reason") else "")
        print(f"[alert] push to {urlparse(url).hostname} failed: {error}")
        return error


# ---------------------------------------------------------------- strip protocol

class Device:
    def __init__(self, mac: str, model: str, fw: str, ip: str):
        self.mac = mac.upper()
        self.model = model
        self.fw = fw
        self.ip = ip
        self.online = False
        self.voltage_v: float | None = None
        self.rssi: int | None = None
        self.outlets = {n: {"n": n, "on": False, "power_w": 0.0, "energy_kwh": 0.0, "temp_c": 0} for n in range(1, 5)}
        self.counter_wh: dict[int, int] = {}       # last cumulative energy counter per outlet
        self.counter_ts: float | None = None
        self.glitches: dict[int, int] = {}         # consecutive implausible counter jumps per outlet
        self.reported_at: float | None = None     # time of the last complete getinfo
        self.last_getinfo = ""
        self.offline_since: float | None = None
        self.offline_alerted = False
        self.raised: set[tuple] = set()           # alarms already reported (edge-triggered)

    @property
    def name(self) -> str:
        return f"MTTL {self.mac[-7:]}"

    @property
    def current_a(self) -> float:
        if not self.voltage_v:
            return 0.0
        return round(sum(o["power_w"] for o in self.outlets.values()) / self.voltage_v, 2)

    def snapshot(self) -> dict:
        outlets = [dict(o) for o in self.outlets.values()]
        return {
            "mac": self.mac,
            "name": self.name,
            "model": self.model,
            "fw": self.fw,
            "ip": self.ip,
            "online": self.online,
            "on": any(o["on"] for o in outlets),
            "power_w": round(sum(o["power_w"] for o in outlets), 2),
            "energy_kwh": round(sum(o["energy_kwh"] for o in outlets), 3),
            "voltage": self.voltage_v,
            "current_a": self.current_a,
            "rssi": self.rssi,
            "reported_at": int(self.reported_at) if self.reported_at else None,
            "outlets": outlets,
        }


class Session:
    """One connected strip. Only one command transaction runs at a time."""

    def __init__(self, ip: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.ip = ip
        self.reader = reader
        self.writer = writer
        self.lock = asyncio.Lock()
        self.device: Device | None = None
        self.got_getinfo = asyncio.Event()
        self.last_cmd = ""
        self.last_rx = ""
        self.misses = 0                 # consecutive unanswered polls

    async def send(self, cmd: str) -> None:
        self.last_cmd = cmd
        self.writer.write((cmd + "\r\n").encode("ascii"))
        await self.writer.drain()

    async def _refresh(self, timeout: float = 6.0) -> bool:
        self.got_getinfo.clear()
        await self.send("up:getinfo:all")
        try:
            await asyncio.wait_for(self.got_getinfo.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def refresh(self, timeout: float = 4.0) -> bool:
        async with self.lock:
            return await self._refresh(timeout)

    async def set_outlets(self, channels: list[int], on: bool) -> bool:
        """Send the commands; True only when a fresh snapshot shows every outlet in the wanted state."""
        async with self.lock:
            for ch in channels:
                await self.send(f"up:onoff:{ch}:{'on' if on else 'off'}")
            await asyncio.sleep(0.3)          # let the relay settle before the snapshot
            if not await self._refresh():
                return False
            return bool(self.device) and all(self.device.outlets[ch]["on"] == on for ch in channels)

    async def diagnostics(self) -> None:
        async with self.lock:
            await self.send("up:power_report:1:vol")
            await self.send("up:query:wifirssi")


class Hub:
    def __init__(self, store: Store | None = None, db: Db | None = None, notifier: Notifier | None = None,
                 allow: set[str] | None = None) -> None:
        self.devices: dict[str, Device] = {}
        self.sessions: dict[str, Session] = {}
        self.store = store or Store(None)
        self.db = db or Db(":memory:")
        self.notifier = notifier or Notifier(self.store)
        self.allow = allow or set()           # empty = accept any strip
        self.observers: list = []             # f(device, now) after every complete reading

    def session(self, mac: str) -> Session | None:
        s = self.sessions.get(mac.upper())
        if s and s.device and s.device.online and not s.writer.is_closing():
            return s
        return None

    def label(self, d: Device) -> str:
        return self.store.name(d.mac, 0) or d.name

    def outlet_label(self, d: Device, n: int) -> str:
        return self.store.name(d.mac, n) or f"Outlet {n}"

    def event(self, level: str, code: str, text: str, mac: str | None = None, outlet: int | None = None, **params) -> None:
        ev = {"ts": time.time(), "level": level, "code": code, "mac": mac, "outlet": outlet, "text": text, "params": params}
        print(f"[{level}] {text}")
        self.db.add_event(ev)
        if code not in NO_PUSH:
            self.notifier.send(f"powerk: {code.replace('_', ' ')}", text)

    @staticmethod
    def alarms(d: Device, settings: dict) -> tuple[list[str], dict[int, list[str]]]:
        """Active alarms: temp_high (outlet temperature) and power_high (strip total), from our own limits."""
        per = {n: ["temp_high"] if o["temp_c"] >= settings["temp_max_c"] else [] for n, o in d.outlets.items()}
        device = ["temp_high"] if any(per.values()) else []
        if sum(o["power_w"] for o in d.outlets.values()) >= settings["power_max_w"]:
            device.append("power_high")
        return device, per

    def snapshot(self) -> dict:
        settings = self.store.settings()
        price = settings["price_kwh"]
        devices = []
        for d in list(self.devices.values()):               # called from web threads
            snap = d.snapshot()
            alarms, per = self.alarms(d, settings)
            totals = self.db.totals(d.mac)
            snap.update(label=self.store.name(d.mac, 0), alarms=alarms, alarm=(alarms or [None])[0],
                        today_kwh=totals["today_kwh"], month_kwh=totals["month_kwh"],
                        month_cost=round(totals["month_kwh"] * price, 2))
            for o in snap["outlets"]:
                o["name"] = self.store.name(d.mac, o["n"])
                o["alarms"] = per.get(o["n"], [])
                o["alarm"] = (o["alarms"] or [None])[0]
            devices.append(snap)
        month_kwh = self.db.totals()["month_kwh"]
        now = time.time()
        return {"api_version": 2, "server_time": int(now),
                "utc_offset_seconds": int(datetime.fromtimestamp(now).astimezone().utcoffset().total_seconds()),
                "devices": devices, "currency": settings["currency"], "price_kwh": price, "budget": settings["budget"],
                "month_kwh": month_kwh, "month_cost": round(month_kwh * price, 2),
                "unread_alerts": self.db.unread(self.store.meta("events_read_id", 0))}

    def remember(self, d: Device) -> None:
        """Known strips survive a restart, so names/rules/history stay usable while a strip is offline."""
        known = self.store.meta("devices", {})
        if known.get(d.mac) != {"model": d.model, "fw": d.fw}:
            known[d.mac] = {"model": d.model, "fw": d.fw}
            self.store.set_meta("devices", known)

    def restore(self) -> None:
        for mac, info in self.store.meta("devices", {}).items():
            if MAC_RE.match(mac) and (not self.allow or mac in self.allow):
                d = self.devices.setdefault(mac, Device(mac, str(info.get("model", "")), str(info.get("fw", "")), ""))
                d.offline_since = d.offline_since or time.time()      # alerts if it does not come back within a minute

    def on_getinfo(self, d: Device, records: list[dict], now: float) -> None:
        """New outlet readings: store energy deltas, report alarms on their rising edge."""
        since = d.counter_ts
        limit = 50 + MAX_OUTLET_W * (now - since) / 3600 if since else 0
        for r in records:
            wh = round(r["energy_kwh"] * 1000)
            prev = d.counter_wh.get(r["n"])
            d.counter_wh[r["n"]] = wh
            if prev is None:                              # first reading is the baseline, not consumption
                continue
            delta = wh - prev if wh >= prev else wh      # counter went down = strip reset it
            if 0 < delta <= limit:
                self.db.add_energy(d.mac, r["n"], delta, now, since)
                d.glitches.pop(r["n"], None)
            elif delta > limit:
                d.glitches[r["n"]] = d.glitches.get(r["n"], 0) + 1
                if d.glitches[r["n"]] < 3:
                    d.counter_wh[r["n"]] = prev           # keep the trusted baseline for the next reading
                    print(f"[energy] {d.name} outlet {r['n']}: ignored counter jump of {delta} Wh")
                else:
                    d.glitches.pop(r["n"], None)            # the jump persists: new baseline, nothing counted
                    print(f"[energy] {d.name} outlet {r['n']}: counter re-based at {wh} Wh")
        d.counter_ts = d.reported_at = now
        d.outlets = {r["n"]: r for r in records}
        for observe in self.observers:
            observe(d, now)

        settings = self.store.settings()
        _, per = self.alarms(d, settings)
        active = {(code, n) for n, codes in per.items() for code in codes}
        total = sum(o["power_w"] for o in d.outlets.values())
        if total >= settings["power_max_w"]:
            active.add(("power_high", 0))
        elif ("power_high", 0) in d.raised and total >= settings["power_max_w"] * 0.95:
            active.add(("power_high", 0))                  # hysteresis: no flapping right at the limit
        for code, n in sorted(active - d.raised):
            if code == "power_high":
                self.event("alert", code, f"{self.label(d)}: total power {total:.0f} W (limit {settings['power_max_w']} W)",
                           d.mac, 0, watts=round(total, 1))
            else:
                o = d.outlets[n]
                detail = f" {o['temp_c']} C" if code == "temp_high" else ""
                self.event("alert", code, f"{self.label(d)} / {self.outlet_label(d, n)}: {code.replace('_', ' ')}{detail}",
                           d.mac, n, temp_c=o["temp_c"], watts=o["power_w"])
        d.raised = active

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        s = Session(ip, reader, writer)
        print(f"[device] connection from {ip}")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                if len(raw) > 8192:
                    raise ValueError("line too long")
                line = raw.replace(b"\x00", b"").decode("utf-8", "replace").strip("\r\n ")
                s.last_rx = line

                m = BOOTINFO_RE.match(line)
                if m:
                    mac = m.group(2).upper()
                    if self.allow and mac not in self.allow:
                        print(f"[device] {ip} announced {mac}, not in --mac list: closing")
                        break
                    d = self.devices.get(mac) or Device(mac, m.group(1), m.group(4), ip)
                    d.model, d.fw, d.ip, d.online = m.group(1), m.group(4), ip, True
                    d.offline_since = None
                    s.device = d
                    self.devices[mac] = d
                    old = self.sessions.get(mac)
                    if old and old is not s:
                        old.writer.close()
                    self.sessions[mac] = s
                    self.remember(d)
                    print(f"[device] {d.name} model={d.model} fw={d.fw}")
                    asyncio.create_task(s.refresh())
                    continue

                if not s.device:
                    continue

                if line.startswith("up:getinfo:"):
                    records = parse_getinfo(line)
                    if sorted(r["n"] for r in records) == [1, 2, 3, 4]:
                        s.device.last_getinfo = line
                        self.on_getinfo(s.device, records, time.time())
                        s.got_getinfo.set()
                    continue

                if line.lower().startswith("up:power_report:"):
                    m = POWER_REPORT_RE.fullmatch(line)
                    if m and int(m.group(2)) >= 50000:      # mV -> V; sub-50000 values are per-outlet current, unused
                        s.device.voltage_v = round(int(m.group(2)) / 1000.0, 1)
                    continue

                m = QUERY_RE.fullmatch(line)
                if m:
                    s.device.rssi = int(m.group(1))
                    continue

                m = EVENT_RE.fullmatch(line)
                if m:
                    ch, on = int(m.group(1)), m.group(2).lower() == "on"
                    for n in ([ch] if ch else [1, 2, 3, 4]):      # 0 = master button
                        s.device.outlets[n]["on"] = on
                    continue

                if ONOFF_ACK_RE.fullmatch(line):
                    continue
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as err:
            print(f"[device] session error {ip}: {err!r} (last sent {s.last_cmd!r}, last received {s.last_rx!r})")
        finally:
            if s.device:
                if self.sessions.get(s.device.mac) is s:
                    self.sessions.pop(s.device.mac, None)
                    s.device.online = False
                    s.device.offline_since = time.time()
                    print(f"[device] {s.device.name} offline")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def poll_loop(self) -> None:
        n = 0
        while True:
            n += 1
            for s in list(self.sessions.values()):
                if not s.device:
                    continue
                try:
                    if not await s.refresh():
                        s.misses += 1
                        print(f"[device] {s.device.name} poll timeout ({s.misses}/{MAX_MISSES})")
                        if s.misses >= MAX_MISSES:
                            # a strip that lost power never sends FIN; without this it stays "online" forever
                            print(f"[device] {s.device.name} not answering, dropping session")
                            s.writer.close()
                        continue
                    s.misses = 0
                    if n % DIAG_EVERY == 0:
                        await s.diagnostics()
                except Exception as err:
                    print(f"[device] poll error: {err!r}")
            await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- rules

RULE_KINDS = ("schedule", "timer", "cycle", "autooff")
RULE_FIELDS = {
    "schedule": ("at", "days", "on"),
    "timer": ("fire_at", "on"),
    "cycle": ("on_min", "off_min"),
    "autooff": ("below_w", "for_min"),
}


def _int_in(value, lo: int, hi: int, field: str) -> int:
    if type(value) is not int or not lo <= value <= hi:          # bool and 1.0 are rejected on purpose
        raise ValueError(f"{field} must be a whole number {lo}-{hi}")
    return value


def _is_outlet(value, lo: int = 1) -> bool:
    return type(value) is int and lo <= value <= 4


def clean_rule(body: dict, now: float, base: dict | None = None) -> dict:
    """Validate a create (base None) or partial update (base = stored rule). Returns the stored form."""
    if not isinstance(body, dict):
        raise ValueError("rule must be an object")
    r = dict(base or {})
    r.update({k: v for k, v in body.items() if k not in ("id", "next_at", "phase", "fire_at")})   # fire_at only from minutes
    kind = r.get("kind")
    if kind not in RULE_KINDS or (base and kind != base["kind"]):
        raise ValueError("kind must be schedule, timer, cycle or autooff (and cannot change)")
    mac = str(r.get("mac", "")).upper()
    if not MAC_RE.match(mac):
        raise ValueError("mac must be 12 hex digits")
    outlets = r.get("outlets")
    if not isinstance(outlets, list) or not outlets or not all(_is_outlet(n, 0) for n in outlets):
        raise ValueError("outlets must be a non-empty list of 1-4 (0 = all)")
    outlets = [1, 2, 3, 4] if 0 in outlets else sorted(set(outlets))
    enabled = r.get("enabled", True)
    label = r.get("label", "")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true/false")
    if not isinstance(label, str) or len(label) > NAME_MAX:
        raise ValueError(f"label must be text up to {NAME_MAX} characters")
    out = {"id": r.get("id") or "r_" + secrets.token_hex(3), "kind": kind, "mac": mac, "outlets": outlets,
           "enabled": enabled, "label": label.strip()}

    if kind in ("schedule", "timer"):
        if not isinstance(r.get("on"), bool):
            raise ValueError("on must be true/false")
        out["on"] = r["on"]
    if kind == "schedule":
        if not isinstance(r.get("at"), str) or not HHMM_RE.match(r["at"]):
            raise ValueError("at must be HH:MM (24 h)")
        days = r.get("days")
        if not isinstance(days, list) or not days or any(isinstance(x, bool) or x not in range(7) for x in days):
            raise ValueError("days must be a non-empty list of 0-6 (0 = Monday)")
        out["at"], out["days"] = r["at"], sorted(set(days))
    elif kind == "timer":
        if "minutes" in body:
            out["fire_at"] = round(now + _int_in(body["minutes"], 1, 10080, "minutes") * 60)
        elif base and "fire_at" in base:
            out["fire_at"] = base["fire_at"]
        else:
            raise ValueError("timer needs minutes (1-10080)")
    elif kind == "cycle":
        out["on_min"] = _int_in(r.get("on_min"), 1, 1440, "on_min")
        out["off_min"] = _int_in(r.get("off_min"), 1, 1440, "off_min")
    else:
        w = r.get("below_w")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or not 0 < w <= 3500:
            raise ValueError("below_w must be 0-3500")
        out["below_w"] = float(w)
        out["for_min"] = _int_in(r.get("for_min"), 1, 1440, "for_min")
    return out


def next_schedule(rule: dict, now: float) -> int | None:
    h, m = map(int, rule["at"].split(":"))
    base = datetime.fromtimestamp(now)
    for add in range(8):
        cand = (base + timedelta(days=add)).replace(hour=h, minute=m, second=0, microsecond=0)
        ts = cand.timestamp()
        back = datetime.fromtimestamp(ts)
        if (back.hour, back.minute) != (h, m):             # skipped by a DST jump: the engine never sees it
            continue
        if cand.weekday() in rule["days"] and ts > now:
            return int(ts)
    return None


class Engine:
    """Runs rules, offline alerts and budget alerts once a second in the asyncio loop."""

    def __init__(self, hub: Hub):
        self.hub = hub
        self.store = hub.store
        self.rt: dict[str, dict] = {}          # per-rule runtime: last_key, phase, next_at, below_since
        self.inflight: set[str] = set()        # rules whose action is still running
        self.last_budget_check = 0.0
        hub.observers.append(self.observe)

    def observe(self, d: Device, now: float) -> None:
        """Every reading resets an auto-off countdown that it disproves (outlet off, or at/above the limit)."""
        for rule in self.store.rules():
            if rule["kind"] != "autooff" or not rule["enabled"] or rule["mac"] != d.mac:
                continue
            since = self.rt.setdefault(rule["id"], {}).setdefault("below_since", {})
            for n in rule["outlets"]:
                o = d.outlets[n]
                if not o["on"] or o["power_w"] >= rule["below_w"]:
                    since.pop(n, None)
                else:
                    since.setdefault(n, now)

    def _current(self, rule: dict) -> bool:
        """The rule was not edited, disabled or deleted while its action waited."""
        return self.store.rule(rule["id"]) == rule

    def reset(self, rule_id: str) -> None:
        self.rt.pop(rule_id, None)

    def view(self, rule: dict, now: float | None = None) -> dict:
        """Rule plus read-only runtime fields for the API."""
        now = now or time.time()
        rt = self.rt.get(rule["id"], {})
        v = dict(rule, next_at=None, phase=None)
        if not rule["enabled"]:
            return v
        if rule["kind"] == "schedule":
            v["next_at"] = next_schedule(rule, now)
        elif rule["kind"] == "timer":
            v["next_at"] = int(rule["fire_at"])
        elif rule["kind"] == "cycle":
            v["phase"], v["next_at"] = rt.get("phase"), rt.get("next_at")
        else:
            since = [t for t in list(rt.get("below_since", {}).values()) if t]     # read from web threads
            v["next_at"] = int(min(since) + rule["for_min"] * 60) if since else None
        return v

    async def _switch(self, rule: dict, on: bool, outlets: list[int] | None = None) -> bool | None:
        """None = strip offline (nothing sent), False = sent but not confirmed, True = confirmed."""
        s = self.hub.session(rule["mac"])
        if not s:
            return None
        try:
            return await s.set_outlets(outlets or rule["outlets"], on)
        except Exception as err:
            print(f"[rule] {rule['id']} command failed: {err!r}")
            return False

    def _report(self, rule: dict, result: bool | None, what: str) -> None:
        text = f"{what}: {self._where(rule)} {'on' if rule['on'] else 'off'}"
        params = {"rule_id": rule["id"], "on": rule["on"], "outlets": rule["outlets"]}
        if result:
            self.hub.event("info", "rule_fired", text, rule["mac"], None, **params)
        elif result is None:
            self.hub.event("warn", "rule_skipped", f"{text} - skipped, strip offline", rule["mac"], None, **params)
        else:
            self.hub.event("warn", "rule_failed", f"{text} - sent but not confirmed by the strip", rule["mac"], None, **params)

    def _where(self, rule: dict, outlets: list[int] | None = None) -> str:
        d = self.hub.devices.get(rule["mac"])
        outlets = outlets or rule["outlets"]
        strip = self.hub.label(d) if d else rule["mac"]
        if outlets == [1, 2, 3, 4]:
            return f"{strip} / all outlets"
        names = ", ".join(self.hub.outlet_label(d, n) if d else f"Outlet {n}" for n in outlets)
        return f"{strip} / {names}"

    async def tick(self, now: float, wait: bool = True) -> None:
        """wait=False (the live loop) runs actions as tasks, so one slow strip never makes another rule late."""
        jobs = []
        for rule in self.store.rules():
            if not rule["enabled"]:
                self.rt.pop(rule["id"], None)
                continue
            if rule["id"] in self.inflight:
                continue
            self.inflight.add(rule["id"])
            jobs.append(self._run(rule, now))
        if wait:
            await asyncio.gather(*jobs)
        else:
            for job in jobs:
                asyncio.create_task(job)
        self._offline_alerts(now)
        if now - self.last_budget_check >= 60:
            self.last_budget_check = now
            self._budget(now)

    async def _run(self, rule: dict, now: float) -> None:
        try:
            await getattr(self, "_" + rule["kind"])(rule, now, self.rt.setdefault(rule["id"], {}))
        except Exception as err:
            print(f"[rule] {rule['id']} error: {err!r}")
        finally:
            self.inflight.discard(rule["id"])

    async def _schedule(self, rule: dict, now: float, rt: dict) -> None:
        lt = datetime.fromtimestamp(now)
        key = lt.strftime("%Y-%m-%d %H:%M")
        if lt.weekday() not in rule["days"] or lt.strftime("%H:%M") != rule["at"] or rt.get("last_key") == key:
            return
        rt["last_key"] = key
        if self._current(rule):
            self._report(rule, await self._switch(rule, rule["on"]), f"Schedule {rule['at']}")

    async def _timer(self, rule: dict, now: float, rt: dict) -> None:
        if now < rule["fire_at"]:
            return
        if not self.hub.session(rule["mac"]) and now - rule["fire_at"] < 300:
            return                                          # strip offline: keep waiting up to 5 minutes
        if not self.store.consume_rule(rule):               # consumed before the command: a crash never replays it
            return                                          # edited or deleted meanwhile: the new version decides
        self.rt.pop(rule["id"], None)
        self._report(rule, await self._switch(rule, rule["on"]), "Timer")

    async def _cycle(self, rule: dict, now: float, rt: dict) -> None:
        if "phase" in rt and now < rt["next_at"]:
            return
        phase = "on" if rt.get("phase") != "on" else "off"     # a new or restarted cycle begins with "on"
        if not self._current(rule):
            return
        result = await self._switch(rule, phase == "on")
        if result is not None:
            rt["phase"] = phase
            rt["next_at"] = int(now + rule["on_min" if phase == "on" else "off_min"] * 60)
            if result is False:
                self.hub.event("warn", "rule_failed", f"Cycle {phase}: {self._where(rule)} - sent but not confirmed by the strip",
                               rule["mac"], None, rule_id=rule["id"], on=phase == "on", outlets=rule["outlets"])
        else:
            rt.setdefault("phase", None)
            rt["next_at"] = int(now + 10)                   # strip offline: retry soon, keep the phase

    async def _autooff(self, rule: dict, now: float, rt: dict) -> None:
        d = self.hub.devices.get(rule["mac"])
        since = rt.setdefault("below_since", {})
        if not d or not d.online or not d.reported_at or now - d.reported_at > 30:
            since.clear()                                   # no fresh readings = no proof of low draw
            return
        for n in rule["outlets"]:                         # observe() keeps below_since honest per reading
            started = since.get(n)
            if started is None or now - started < rule["for_min"] * 60 or not self._current(rule):
                continue
            since.pop(n, None)
            watts = d.outlets[n]["power_w"]
            result = await self._switch(rule, False, [n])
            if result:
                self.hub.event("info", "autooff", f"Auto-off: {self._where(rule, [n])} drew {watts} W for {rule['for_min']} min",
                               rule["mac"], n, rule_id=rule["id"], watts=watts)
            elif result is False:
                self.hub.event("warn", "rule_failed", f"Auto-off: {self._where(rule, [n])} - sent but not confirmed by the strip",
                               rule["mac"], n, rule_id=rule["id"], on=False, outlets=[n])

    def _offline_alerts(self, now: float) -> None:
        for d in list(self.hub.devices.values()):
            if not d.online and d.offline_since and not d.offline_alerted and now - d.offline_since >= OFFLINE_ALERT_AFTER:
                d.offline_alerted = True
                self.hub.event("warn", "offline", f"{self.hub.label(d)} went offline", d.mac)
            elif d.online and d.offline_alerted:
                d.offline_alerted = False
                self.hub.event("info", "online", f"{self.hub.label(d)} is back online", d.mac)

    def _budget(self, now: float) -> None:
        s = self.store.settings()
        if not s["budget"]:
            return
        cost = self.hub.db.totals()["month_kwh"] * s["price_kwh"]
        pct = cost / s["budget"] * 100
        month = datetime.fromtimestamp(now).strftime("%Y-%m")
        done = self.store.meta("budget_alerted", {})
        step = max((p for p in BUDGET_STEPS if pct >= p), default=0)
        if step > done.get(month, 0):
            self.store.set_meta("budget_alerted", {month: step})
            self.hub.event("alert" if step >= 90 else "warn", "budget",
                           f"Monthly budget {step}% used: {cost:.2f} of {s['budget']:.2f} {s['currency']}",
                           pct=step, cost=round(cost, 2))

    async def loop(self) -> None:
        while True:
            try:
                await self.tick(time.time(), wait=False)
            except Exception as err:
                print(f"[rule] engine error: {err!r}")
            await asyncio.sleep(1.0)


# ---------------------------------------------------------------- web UI

PAGE = r"""<!doctype html>
<html lang=en dir=ltr data-theme=kraft>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=color-scheme content="light dark">
<meta name=theme-color id=themeColor content="#c9a46e">
<title>powerk</title>
<link rel=icon href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='1' y='1' width='14' height='14' rx='4' fill='%23f7e33b' stroke='%2317130f'/%3E%3Cpath d='M8 4v4M5.5 5.5a3.5 3.5 0 1 0 5 0' stroke='%2317130f' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
/* ---- fonts (SIL Open Font License 1.1), bundled so the page works offline ---- */
@font-face{font-family:'Atkinson Hyperlegible';font-weight:400;font-display:swap;src:url(fonts/AtkinsonHyperlegible-400-latin.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Atkinson Hyperlegible';font-weight:700;font-display:swap;src:url(fonts/AtkinsonHyperlegible-700-latin.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Atkinson Hyperlegible Mono';font-weight:400;font-display:swap;src:url(fonts/AtkinsonHyperlegibleMono-400-latin.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'IBM Plex Sans Arabic';font-weight:400;font-display:swap;src:url(fonts/IBMPlexSansArabic-400-arabic.woff2) format('woff2');unicode-range:U+0600-06FF, U+0750-077F, U+0870-0891, U+0897-08E1, U+08E3-08FF, U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, U+FE76-FEFC, U+102E0-102FB, U+10E60-10E7E, U+10EC2-10EC7, U+10EC9-10EEE, U+10EF0-10EFF, U+1EE00-1EE03, U+1EE05-1EE1F, U+1EE21-1EE22, U+1EE24, U+1EE27, U+1EE29-1EE32, U+1EE34-1EE37, U+1EE39, U+1EE3B, U+1EE42, U+1EE47, U+1EE49, U+1EE4B, U+1EE4D-1EE4F, U+1EE51-1EE52, U+1EE54, U+1EE57, U+1EE59, U+1EE5B, U+1EE5D, U+1EE5F, U+1EE61-1EE62, U+1EE64, U+1EE67-1EE6A, U+1EE6C-1EE72, U+1EE74-1EE77, U+1EE79-1EE7C, U+1EE7E, U+1EE80-1EE89, U+1EE8B-1EE9B, U+1EEA1-1EEA3, U+1EEA5-1EEA9, U+1EEAB-1EEBB, U+1EEF0-1EEF1}
@font-face{font-family:'IBM Plex Sans Arabic';font-weight:600;font-display:swap;src:url(fonts/IBMPlexSansArabic-600-arabic.woff2) format('woff2');unicode-range:U+0600-06FF, U+0750-077F, U+0870-0891, U+0897-08E1, U+08E3-08FF, U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, U+FE76-FEFC, U+102E0-102FB, U+10E60-10E7E, U+10EC2-10EC7, U+10EC9-10EEE, U+10EF0-10EFF, U+1EE00-1EE03, U+1EE05-1EE1F, U+1EE21-1EE22, U+1EE24, U+1EE27, U+1EE29-1EE32, U+1EE34-1EE37, U+1EE39, U+1EE3B, U+1EE42, U+1EE47, U+1EE49, U+1EE4B, U+1EE4D-1EE4F, U+1EE51-1EE52, U+1EE54, U+1EE57, U+1EE59, U+1EE5B, U+1EE5D, U+1EE5F, U+1EE61-1EE62, U+1EE64, U+1EE67-1EE6A, U+1EE6C-1EE72, U+1EE74-1EE77, U+1EE79-1EE7C, U+1EE7E, U+1EE80-1EE89, U+1EE8B-1EE9B, U+1EEA1-1EEA3, U+1EEA5-1EEA9, U+1EEAB-1EEBB, U+1EEF0-1EEF1}
@font-face{font-family:'Kalam';font-weight:700;font-display:swap;src:url(fonts/Kalam-700-latin.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Marhey';font-weight:700;font-display:swap;src:url(fonts/Marhey-700-arabic.woff2) format('woff2');unicode-range:U+0600-06FF, U+0750-077F, U+0870-0891, U+0897-08E1, U+08E3-08FF, U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, U+FE76-FEFC, U+102E0-102FB, U+10E60-10E7E, U+10EC2-10EC7, U+10EC9-10EEE, U+10EF0-10EFF, U+1EE00-1EE03, U+1EE05-1EE1F, U+1EE21-1EE22, U+1EE24, U+1EE27, U+1EE29-1EE32, U+1EE34-1EE37, U+1EE39, U+1EE3B, U+1EE42, U+1EE47, U+1EE49, U+1EE4B, U+1EE4D-1EE4F, U+1EE51-1EE52, U+1EE54, U+1EE57, U+1EE59, U+1EE5B, U+1EE5D, U+1EE5F, U+1EE61-1EE62, U+1EE64, U+1EE67-1EE6A, U+1EE6C-1EE72, U+1EE74-1EE77, U+1EE79-1EE7C, U+1EE7E, U+1EE80-1EE89, U+1EE8B-1EE9B, U+1EEA1-1EEA3, U+1EEA5-1EEA9, U+1EEAB-1EEBB, U+1EEF0-1EEF1}
@font-face{font-family:'Marhey';font-weight:700;font-display:swap;src:url(fonts/Marhey-700-latin.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Permanent Marker';font-weight:400;font-display:swap;src:url(fonts/PermanentMarker-400-latin.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}

/* ---- themes: kraft paper (default), clean white/blue (follows light/dark), chalk on black ---- */
:root{--bg:#c9a46e;--card:#ece0b8;--fill:#d8b983;--ink:#17130f;--ink2:#3a2e20;--line:#b48c55;
  --accent:#f7e33b;--accent-ink:#17130f;--accent-sub:#3a2e20;--accent-soft:#f3e690;--accent-text:#083f3a;
  --hl:#f7e33b;--hl-ink:#17130f;--tape:rgba(236,224,184,.8);--chart:#1c9a90;--switch-on:#1c9a90;--focus:#17130f;
  --ok:#0b6b62;--ok-soft:#cfe0c8;--warn:#7d4306;--warn-soft:#f2dca0;--bad:#9c2318;--bad-soft:#efcbb5;
  --shadow:0 1px 0 rgba(23,19,15,.16),0 10px 20px -12px rgba(58,46,32,.55);--scrim:rgba(23,19,15,.45);--bar:rgba(216,185,131,.95);
  --r:20px;--r-s:14px;--wobble:255px 15px 225px 15px / 15px 225px 15px 255px;--wobble2:15px 225px 15px 255px / 255px 15px 225px 15px;
  --font:"Atkinson Hyperlegible","IBM Plex Sans Arabic",system-ui,sans-serif;--marker:"Permanent Marker","Marhey",cursive;
  --hand:"Kalam","Marhey",cursive;--mono:"Atkinson Hyperlegible Mono","IBM Plex Sans Arabic",ui-monospace,monospace;
  font-synthesis:none;color-scheme:light}
:root[data-theme=clean]{--bg:#F2F2F7;--card:#FFFFFF;--fill:#E9E9EF;--ink:#111827;--ink2:#5B6170;--line:#E3E4E9;
  --accent:#2563EB;--accent-ink:#FFFFFF;--accent-sub:rgba(255,255,255,.9);--accent-soft:#E2EAFD;--accent-text:#1D4ED8;
  --hl:var(--accent-soft);--hl-ink:var(--ink);--tape:rgba(37,99,235,.18);--chart:#2563EB;--switch-on:#15803D;--focus:#2563EB;
  --ok:#15803D;--ok-soft:#DCF5E4;--warn:#B45309;--warn-soft:#FDF0D5;--bad:#DC2626;--bad-soft:#FDE4E4;
  --shadow:0 1px 2px rgba(17,24,39,.05),0 6px 16px -8px rgba(17,24,39,.10);--scrim:rgba(17,24,39,.38);--bar:rgba(255,255,255,.86);color-scheme:light}
@media (prefers-color-scheme:dark){:root[data-theme=clean]:not([data-scheme=light]){--bg:#0B0B0F;--card:#1C1C21;--fill:#2A2A31;--ink:#F4F4F6;--ink2:#A3A3AE;--line:#2E2E36;--accent-soft:#1D2947;--accent-text:#93B6FF;--tape:rgba(147,182,255,.22);--switch-on:#30A852;--focus:#93B6FF;--ok:#4ADE80;--ok-soft:#12301D;--warn:#FBBF24;--warn-soft:#352A0E;--bad:#F87171;--bad-soft:#3A1818;--shadow:0 0 0 1px #24242B;--scrim:rgba(0,0,0,.6);--bar:rgba(22,22,27,.86);color-scheme:dark}}
:root[data-theme=clean][data-scheme=dark]{--bg:#0B0B0F;--card:#1C1C21;--fill:#2A2A31;--ink:#F4F4F6;--ink2:#A3A3AE;--line:#2E2E36;--accent-soft:#1D2947;--accent-text:#93B6FF;--tape:rgba(147,182,255,.22);--switch-on:#30A852;--focus:#93B6FF;--ok:#4ADE80;--ok-soft:#12301D;--warn:#FBBF24;--warn-soft:#352A0E;--bad:#F87171;--bad-soft:#3A1818;--shadow:0 0 0 1px #24242B;--scrim:rgba(0,0,0,.6);--bar:rgba(22,22,27,.86);color-scheme:dark}
:root[data-theme=night]{--bg:#0B0B0F;--card:#1C1C21;--fill:#2A2A31;--ink:#f3eee2;--ink2:#cfc8b8;--line:#33333B;
  --accent:#f7e33b;--accent-ink:#17130f;--accent-sub:#3a2e20;--accent-soft:#3a3816;--accent-text:#f7e33b;
  --hl:#f7e33b;--hl-ink:#17130f;--tape:rgba(236,224,184,.85);--chart:#1c9a90;--switch-on:#1c9a90;--focus:#f7e33b;
  --ok:#5fd3c6;--ok-soft:#163a36;--warn:#f0b54a;--warn-soft:#3a2f14;--bad:#ff8f7f;--bad-soft:#3e1e19;
  --shadow:0 0 0 1px #26262D,0 12px 24px -14px rgba(0,0,0,.7);--scrim:rgba(0,0,0,.6);--bar:rgba(11,11,15,.94);color-scheme:dark}

*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{margin:0 auto;max-width:760px;min-height:100vh;padding:max(12px,env(safe-area-inset-top)) 16px calc(100px + env(safe-area-inset-bottom));
  background:var(--bg);color:var(--ink);font:16px/1.5 var(--font);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
::selection{background:var(--accent-soft);color:var(--ink)}
:focus-visible{outline:2.5px solid var(--focus);outline-offset:2px;border-radius:10px}
html{scrollbar-color:var(--line) transparent}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0}
svg.i{width:20px;height:20px;flex:none;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
bdi,.ltr{unicode-bidi:isolate;direction:ltr}

/* ---- top bar ---- */
.top{display:flex;align-items:center;gap:10px;min-height:44px;margin-bottom:6px}
.brand{display:flex;align-items:center;gap:8px;font-size:17px}
.brand i{width:26px;height:26px;border-radius:8px;background:var(--accent);display:grid;place-items:center;color:var(--accent-ink)}
.brand i svg.i{width:16px;height:16px;stroke-width:2.2}
.chip{margin-inline-start:auto;display:inline-flex;align-items:center;height:34px;padding:0 14px;border-radius:999px;background:var(--fill);font-size:14px;font-weight:600}
.chip:hover{background:var(--line)}

/* ---- nav: tab bar on phones, pill row on wide screens ---- */
nav{position:fixed;inset-inline:0;bottom:0;z-index:5;display:flex;justify-content:space-around;padding:6px 6px calc(6px + env(safe-area-inset-bottom));
  background:var(--bar);-webkit-backdrop-filter:saturate(1.6) blur(18px);backdrop-filter:saturate(1.6) blur(18px);border-top:1px solid var(--line)}
nav button{position:relative;flex:1;max-width:96px;display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 2px;border-radius:12px;color:var(--ink2);font-size:11.5px;font-weight:600}
nav button svg.i{width:25px;height:25px}
nav button[aria-current=page]{color:var(--accent-text)}
.badge{position:absolute;top:1px;inset-inline-start:calc(50% + 4px);min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--bad);color:#fff;font-size:11px;font-weight:700;line-height:18px;text-align:center}
@media (min-width:820px){
  body{padding-bottom:40px}
  nav{position:static;justify-content:flex-start;gap:4px;padding:4px;margin:6px 0 22px;width:max-content;border:0;border-radius:999px;background:var(--fill);-webkit-backdrop-filter:none;backdrop-filter:none}
  nav button{flex:none;max-width:none;flex-direction:row;gap:8px;padding:8px 16px;font-size:15px;border-radius:999px}
  nav button svg.i{width:19px;height:19px}
  nav button[aria-current=page]{background:var(--card);color:var(--ink);box-shadow:var(--shadow)}
  .badge{position:static;margin-inline-start:2px}
}

/* ---- page type and surfaces ---- */
h1{font-size:30px;line-height:1.15;font-weight:750;letter-spacing:-.02em;margin:0;overflow-wrap:anywhere}
h2{font-size:20px;line-height:1.25;font-weight:700;letter-spacing:-.01em;margin:0}
h3{font-size:13px;font-weight:600;color:var(--ink2);margin:0 0 8px}
.card{background:var(--card);border-radius:var(--r);padding:18px;margin-bottom:14px;box-shadow:var(--shadow)}
.card>h2{margin-bottom:12px}
.muted{color:var(--ink2);font-size:14px}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.iconbtn{width:40px;height:40px;flex:none;display:inline-grid;place-items:center;border-radius:12px;color:var(--ink2)}
.iconbtn:hover{background:var(--fill);color:var(--ink)}
.state{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 11px;border-radius:999px;font-size:13px;font-weight:650;white-space:nowrap;background:var(--bad-soft);color:var(--bad)}
.state::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.state.on{background:var(--ok-soft);color:var(--ok)}
.pill{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 12px;border-radius:999px;font-size:13px;font-weight:650;background:var(--bad-soft);color:var(--bad)}
.pill svg.i{width:16px;height:16px}

/* ---- outlets ---- */
.dev{margin-bottom:28px}
.devhead{display:flex;align-items:center;gap:6px;margin:6px 0 14px}
.devhead h1{min-width:0}
.devhead .state{margin-inline-start:auto}
.alarms{margin:-4px 0 12px}
.sum small{display:block;font-size:13px;color:var(--ink2);font-weight:600}
.sum strong{display:block;font-size:42px;line-height:1.1;font-weight:750;letter-spacing:-.03em;margin:2px 0 8px}
.sum strong span{font-size:20px;font-weight:650;margin-inline-start:4px;letter-spacing:0}
.mini{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:14px;color:var(--ink2)}
.mini b{color:var(--ink);font-weight:600}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media (min-width:640px){.tiles{grid-template-columns:repeat(4,1fr)}}
.tile{position:relative;min-height:152px;border-radius:var(--r);background:var(--card);color:var(--ink);box-shadow:var(--shadow);transition:background-color .28s cubic-bezier(.2,.8,.2,1),color .28s}
.tile.on{background:var(--accent);color:var(--accent-ink)}
.tile .tap{position:absolute;inset:0;width:100%;display:flex;flex-direction:column;justify-content:space-between;padding:16px;text-align:start;border-radius:inherit;transition:transform .15s}
.tile .tap:active{transform:scale(.97)}
.tile .tap[disabled]{cursor:default}
.tile .tap[disabled]:active{transform:none}
.tname{font-weight:650;font-size:16px;line-height:1.3;padding-inline-end:30px;overflow-wrap:anywhere}
.tname small{display:block;font-size:13px;font-weight:500;color:var(--ink2);margin-top:2px}
.tile.on .tname small{color:var(--accent-sub)}
.tname small .alarm{color:var(--bad);font-weight:650}
.tile.on .tname small .alarm{color:var(--accent-ink);text-decoration:underline;text-underline-offset:3px}
.tfoot{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tw{font-size:20px;font-weight:700;letter-spacing:-.01em}
.tw small{font-size:13px;font-weight:600;margin-inline-start:2px}
.knob{position:relative;width:44px;height:26px;flex:none;border-radius:999px;background:var(--fill);transition:background-color .25s}
.knob::after{content:"";position:absolute;top:3px;inset-inline-start:3px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.22);transition:inset-inline-start .25s cubic-bezier(.2,.8,.2,1)}
.tile.on .knob{background:color-mix(in srgb,var(--accent-ink) 28%,transparent)}
.tile.on .knob::after{inset-inline-start:21px}
.tile.pending .knob::after{inset-inline-start:12px;animation:wait 1s ease-in-out infinite alternate}
@keyframes wait{from{opacity:1}to{opacity:.45}}
.tile .edit{position:absolute;top:8px;inset-inline-end:8px;z-index:1;width:34px;height:34px;display:grid;place-items:center;border-radius:10px;color:var(--ink2)}
.tile.on .edit{color:var(--accent-ink)}
.tile .edit svg.i{width:17px;height:17px}
.tile .edit:hover{background:var(--fill)}
.tile.on .edit:hover{background:color-mix(in srgb,var(--accent-ink) 14%,transparent)}
.dev.offline .tile,.dev.offline .sum{opacity:.55}
.both{display:grid;grid-template-columns:1fr 1fr;gap:12px}

/* ---- buttons & fields ---- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:48px;padding:0 20px;white-space:nowrap;border-radius:var(--r-s);font-weight:650;background:var(--card);color:var(--ink);box-shadow:var(--shadow);transition:filter .15s,transform .15s}
.btn:hover{filter:brightness(.97)}
.btn:active{transform:scale(.98)}
.btn.dark{background:var(--ink);color:var(--bg);box-shadow:none}
.btn.primary{background:var(--accent);color:var(--accent-ink);box-shadow:none}
.btn.plain{background:var(--fill);box-shadow:none}
.btn.danger{background:var(--bad-soft);color:var(--bad);box-shadow:none}
.btn[disabled]{opacity:.45;cursor:default;transform:none}
.btn svg.i{width:19px;height:19px}
.seg{display:flex;padding:3px;gap:2px;border-radius:12px;background:var(--fill);overflow-x:auto;scrollbar-width:none}
.seg button{flex:1;height:34px;padding:0 12px;border-radius:9px;font-size:14px;font-weight:600;color:var(--ink2);white-space:nowrap}
.seg button[aria-pressed=true]{background:var(--card);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.12)}
label.f{display:grid;gap:6px;font-size:13px;font-weight:600;color:var(--ink2);margin-bottom:14px}
input,select{height:48px;width:100%;padding:0 14px;border-radius:12px;border:1px solid transparent;background:var(--fill);color:var(--ink);font-size:16px;caret-color:var(--focus)}
input::placeholder{color:var(--ink2)}
input:focus,select:focus{outline:none;border-color:var(--focus);background:var(--card)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
.checks{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.checks label{position:relative}
.checks input{position:absolute;opacity:0;width:1px;height:1px}
.checks span{display:inline-flex;align-items:center;height:38px;padding:0 14px;border-radius:999px;background:var(--fill);font-size:14px;font-weight:600;color:var(--ink2);transition:background-color .15s}
.checks input:checked+span{background:var(--accent);color:var(--accent-ink)}
.checks input:focus-visible+span{outline:2.5px solid var(--focus);outline-offset:2px}
.switch{position:relative;width:51px;height:31px;flex:none;border-radius:999px;background:var(--fill);transition:background-color .25s}
.switch::after{content:"";position:absolute;top:2px;inset-inline-start:2px;width:27px;height:27px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);transition:inset-inline-start .25s cubic-bezier(.2,.8,.2,1)}
.switch[aria-checked=true]{background:var(--switch-on)}
.switch[aria-checked=true]::after{inset-inline-start:22px}

/* ---- lists ---- */
.list>*+*{border-top:1px solid var(--line)}
.item{display:flex;align-items:center;gap:12px;padding:12px 0}
.item .grow{flex:1;min-width:0}
.item b{display:block;font-weight:650}
.dotk{width:38px;height:38px;flex:none;display:grid;place-items:center;border-radius:11px;background:var(--accent-soft);color:var(--accent-text)}
.ev.warn .dotk{background:var(--warn-soft);color:var(--warn)}
.ev.alert .dotk{background:var(--bad-soft);color:var(--bad)}
.ev.info .dotk{background:var(--fill);color:var(--ink2)}
.empty{padding:22px 4px;text-align:center;color:var(--ink2)}

/* ---- energy ---- */
.total{font-size:36px;line-height:1.1;font-weight:750;letter-spacing:-.03em;margin:18px 0 2px}
.total span{font-size:18px;font-weight:650;letter-spacing:0;margin-inline-start:4px}
.bars{display:flex;align-items:flex-end;gap:4px;height:170px;margin:18px 0 6px;border-bottom:1px solid var(--line)}
.bars div{flex:1;min-height:3px;border-radius:3px 9px 2px 2px;background:var(--chart);transition:height .5s cubic-bezier(.2,.8,.2,1)}
.bars div.zero{background:var(--line)}
.axis{display:flex;justify-content:space-between;font-size:12px;color:var(--ink2)}
.meter{height:10px;border-radius:5px;background:var(--fill);overflow:hidden;margin:10px 0 6px}
.meter div{height:100%;border-radius:5px;background:var(--ok);transition:width .6s cubic-bezier(.2,.8,.2,1)}
.legend{margin-top:14px}
.legend div{display:flex;justify-content:space-between;gap:8px;padding:9px 0;font-size:15px}
.legend div+div{border-top:1px solid var(--line)}
.legend b{font-weight:600}

/* ---- toast & dialog ---- */
.toast{position:fixed;inset-inline:16px;bottom:calc(92px + env(safe-area-inset-bottom));z-index:9;max-width:480px;margin:auto;padding:12px 16px;border-radius:14px;background:var(--ink);color:var(--bg);font-weight:600;font-size:15px;
  box-shadow:0 8px 24px -8px rgba(0,0,0,.35);transform:translateY(16px);opacity:0;transition:transform .25s cubic-bezier(.2,.8,.2,1),opacity .25s;pointer-events:none}
.toast.show{transform:none;opacity:1}
@media (min-width:820px){.toast{bottom:28px}}
dialog{border:0;border-radius:var(--r);padding:22px;width:min(92vw,400px);background:var(--card);color:var(--ink);box-shadow:0 24px 60px rgba(0,0,0,.3)}
dialog::backdrop{background:var(--scrim)}
.skeleton{min-height:152px;border-radius:var(--r);background:linear-gradient(90deg,var(--card),var(--fill),var(--card));background-size:200% 100%;animation:sk 1.2s infinite}
@keyframes sk{to{background-position:-200% 0}}
/* ---- type: marker headings, hand-written labels, mono figures ---- */
h1,h2,.brand,.sum strong,.total,.tname{font-family:var(--marker);font-weight:700;letter-spacing:0;text-transform:uppercase}
.tname small{font-family:var(--font);text-transform:none;font-weight:600}
h3,.sum small,.legend span,.state{font-family:var(--hand);font-weight:700}
h3{font-size:15px}
.tw,.mini b,.legend b,.axis,.chip,input[type=number],input[type=time]{font-family:var(--mono)}
.btn,nav button,.seg button{font-family:var(--hand);font-weight:700;font-size:15px}
nav button{font-size:12.5px}

/* ---- shapes: hand-cut edges, tape, highlighter, dashed rules, a slight tilt, paper grain ---- */
html,body{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 .18 0 0 0 0 .13 0 0 0 0 .07 0 0 0 .1 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.card{border-radius:var(--wobble);border:1.5px solid var(--ink)}
.tile{border-radius:var(--wobble2);border:2px solid var(--ink);box-shadow:none}
.tile:nth-child(odd){transform:rotate(-.7deg)}
.tile:nth-child(even){transform:rotate(.6deg)}
.tile .edit:hover{border-radius:50%}
.sum{position:relative;transform:rotate(-.4deg)}
.sum::before,.sum::after{content:"";position:absolute;height:24px;background:var(--tape);box-shadow:0 1px 2px rgba(0,0,0,.18)}
.sum::before{top:-12px;inset-inline-start:28px;width:92px;transform:rotate(-5deg)}
.sum::after{top:-10px;inset-inline-end:34px;width:70px;height:22px;transform:rotate(4deg)}
h1{width:fit-content;padding:0 .18em;margin-inline-start:-.18em;border-radius:4px 10px 6px 12px;background:linear-gradient(transparent 52%,var(--hl) 52% 90%,transparent 90%)}
:root[data-theme=night] h1{background:var(--hl);color:var(--hl-ink);padding:.04em .3em}
.btn{border:2px solid var(--ink);border-radius:var(--wobble2);box-shadow:none}
.both .btn:nth-child(1){transform:rotate(-.6deg)}
.both .btn:nth-child(2){transform:rotate(.5deg)}
.seg{background:transparent;border:1.5px solid var(--ink);border-radius:var(--wobble)}
.seg button{border-radius:var(--wobble2)}
.seg button[aria-pressed=true]{background:var(--hl);color:var(--hl-ink);box-shadow:none}
.checks span{border:1.5px dashed var(--ink2);background:transparent;border-radius:var(--wobble2)}
.checks input:checked+span{border-style:solid;border-color:var(--ink)}
input,select{border:1.5px solid var(--ink2);border-radius:6px 12px 8px 14px;background:transparent}
.list>*+*,.legend div+div{border-top:1.5px dashed var(--line)}
nav{border-top:1.5px dashed var(--ink2)}
.state{border:1.5px solid currentColor;background:transparent}
.dotk{border-radius:var(--wobble2);border:1.5px solid currentColor}
dialog{border-radius:var(--wobble);border:2px solid var(--ink)}
.skeleton{border-radius:var(--wobble2)}
@media (min-width:820px){nav{border:1.5px solid var(--ink);border-radius:var(--wobble);background:transparent}
  nav button[aria-current=page]{background:var(--hl);color:var(--hl-ink);box-shadow:none}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}.tile,.sum,.btn{transform:none!important}}
</style>
<body>
<header class=top>
  <span class=brand><i><svg class=i viewBox="0 0 24 24" aria-hidden=true><path d="M12 4v7M7.4 7.2a7 7 0 1 0 9.2 0"/></svg></i>powerk</span>
  <button class=chip id=langBtn onclick="setLang(lang==='ar'?'en':'ar')"></button>
</header>
<nav id=nav></nav>
<main id=app></main>
<div class=toast id=toast role=status aria-live=polite></div>
<dialog id=dlg><form method=dialog id=dlgForm></form></dialog>
<script>
const ICON={
 plug:'<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0V7zM12 16v5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 cal:'<rect x="4" y="5" width="16" height="15" rx="3"/><path d="M4 10h16M9 3v4M15 3v4"/>',
 cycle:'<path d="M20 12a8 8 0 0 1-14 5.3M4 12a8 8 0 0 1 14-5.3M18 3v4h-4M6 21v-4h4"/>',
 chart:'<path d="M5 20V11M10.5 20V5M16 20v-6M21 20H3"/>',
 bell:'<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2zM10 20a2 2 0 0 0 4 0"/>',
 gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
 pen:'<path d="M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4"/>',
 power:'<path d="M12 3v8M6.3 6.3a8 8 0 1 0 11.4 0"/>',
 trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 warn:'<path d="M12 3l10 18H2L12 3zM12 10v4M12 17.5v.5"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
 wifi:'<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 19.5v.5"/>',
 send:'<path d="M21 3L10 14M21 3l-7 18-4-7-7-4 18-7z"/>',
};
const ic=(n,c='i')=>`<svg class="${c}" viewBox="0 0 24 24" aria-hidden=true>${ICON[n]}</svg>`;

const T={
 en:{tabs:['Outlets','Rules','Energy','Alerts','Settings'],
  now:'Using now',outlet:'Outlet',allOn:'All on',allOff:'All off',online:'Online',offline:'Offline',on:'On',off:'Off',sending:'Switching…',today:'Today',month:'This month',stripInfo:'Strip info',conn:'Connection',wifi:'Wi-Fi signal',wifiLv:['Excellent','Good','Weak','Very weak — the strip may drop'],wifiHelp:'The closer the number is to zero, the stronger the signal.',ipAddr:'Address on your network',model:'Model',fw:'Firmware version',mac:'Device ID (MAC)',
  rename:'Rename',stripName:'Strip name',outletName:'Outlet name',resetHint:'Leave empty for the default name.',save:'Save',cancel:'Cancel',
  noStrip:'No strip connected yet',noStripBody:'Provision the strip with this server’s IP, then power-cycle it.',notConfirmed:'Sent, but the strip did not confirm',
  rules:'Rules',rulesEmpty:'No rules yet. Rules run on the server, even while your phone is off.',newRule:'New rule',kinds:{schedule:'Schedule',timer:'Timer',cycle:'Cycle',autooff:'Auto-off'},
  at:'At',action:'Action',turnOn:'Turn on',turnOff:'Turn off',inMin:'In (minutes)',onMin:'On (min)',offMin:'Off (min)',belowW:'Below (W)',forMin:'For (min)',label:'Label (optional)',
  outlets:'Outlets',days:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],repeat:'Repeat',everyDay:'Every day',allOutlets:'All outlets',addRule:'Add rule',added:'Rule added',delRule:'Delete this rule?',next:'next',
  cycleDesc:(a,b)=>`${a} min on / ${b} min off`,autoDesc:(w,m)=>`Off below ${w} W for ${m} min`,timerAt:t=>`at ${t}`,nowPhase:p=>`now ${p}`,strip:'Strip',
  ranges:[['day','24 h'],['week','7 days'],['month','30 days'],['year','12 months']],budget:'Monthly budget',noBudget:'No monthly budget set. Add one in Settings.',ofBudget:(p,b)=>`${p}% of ${b}`,
  estimate:'Estimated from the strip’s own meter, not an electricity bill.',at_price:p=>`at ${p}/kWh`,byOutlet:'By outlet',
  alerts:'Alerts & activity',alertsEmpty:'Nothing has happened yet.',
  cost:'Cost',price:'Price per kWh',currency:'Currency',budgetF:'Monthly budget (empty = off)',limits:'Limits',tempMax:'Max outlet temperature (°C)',powerMax:'Max total power (W)',
  push:'Push notifications',pushHelp:'ntfy: subscribe to a long, random topic in the ntfy app and paste its URL. Telegram: a bot token from @BotFather and your chat id.',
  ntfy:'ntfy topic URL',tgToken:'Telegram bot token',tgChat:'Telegram chat id',tgSaved:'Saved — type to replace',clearTg:'Remove token',sendTest:'Send test',saved:'Saved',
  sentVia:s=>`Sent via ${s}`,noChannel:'No push channel configured',failed:'failed',appearance:'Appearance',language:'Language',theme:'Theme',themes:{kraft:'Kraft paper',clean:'White & blue',night:'Chalkboard'},scheme:'Light or dark',schemes:{auto:'System',light:'Light',dark:'Dark'},
  ev:{offline:'Strip went offline',online:'Strip is back online',temp_high:p=>`Temperature high: ${p.temp_c} °C`,power_high:p=>`Total power high: ${p.watts} W`,
   budget:p=>`Monthly budget ${p.pct}% used`,rule_fired:p=>`Rule ran: switched ${p.on?'on':'off'}`,rule_skipped:'Rule skipped: strip offline',rule_failed:'Rule sent but not confirmed',autooff:p=>`Auto-off at ${p.watts} W`},
  alarm:{temp_high:'Hot',power_high:'Power high'}},
 ar:{tabs:['الفيش','القواعد','الاستهلاك','التنبيهات','الإعدادات'],
  now:'الاستهلاك دلوقتي',outlet:'فيشة',allOn:'شغّل الكل',allOff:'اطفي الكل',online:'متصل',offline:'غير متصل',on:'شغالة',off:'طافية',sending:'بيتنفذ…',today:'النهارده',month:'الشهر ده',stripInfo:'معلومات المشترك',conn:'الاتصال',wifi:'إشارة الواي فاي',wifiLv:['ممتازة','كويسة','ضعيفة شوية','ضعيفة — ممكن يفصل'],wifiHelp:'كل ما الرقم يقرب من الصفر، الإشارة بتبقى أقوى.',ipAddr:'عنوانه على الشبكة',model:'الموديل',fw:'إصدار البرنامج اللي جواه',mac:'رقم الجهاز (MAC)',
  rename:'تغيير الاسم',stripName:'اسم المشترك',outletName:'اسم الفيشة',resetHint:'سيبه فاضي للاسم الافتراضي.',save:'حفظ',cancel:'إلغاء',
  noStrip:'مفيش مشترك متصل لسه',noStripBody:'اعمل تهيئة للمشترك بعنوان السيرفر ده، وبعدين افصله ووصله تاني.',notConfirmed:'الأمر اتبعت بس المشترك ما أكدش',
  rules:'القواعد',rulesEmpty:'مفيش قواعد لسه. القواعد بتشتغل على السيرفر حتى لو الموبايل مقفول.',newRule:'قاعدة جديدة',kinds:{schedule:'جدول',timer:'مؤقت',cycle:'دورة',autooff:'إطفاء تلقائي'},
  at:'الساعة',action:'الأمر',turnOn:'تشغيل',turnOff:'إطفاء',inMin:'بعد (دقيقة)',onMin:'تشغيل (دقيقة)',offMin:'إطفاء (دقيقة)',belowW:'أقل من (واط)',forMin:'لمدة (دقيقة)',label:'اسم القاعدة (اختياري)',
  outlets:'الفيش',days:['الاتنين','التلات','الأربع','الخميس','الجمعة','السبت','الحد'],repeat:'التكرار',everyDay:'كل يوم',allOutlets:'كل الفيش',addRule:'إضافة القاعدة',added:'القاعدة اتضافت',delRule:'تمسح القاعدة دي؟',next:'الجاية',
  cycleDesc:(a,b)=>`${a} دقيقة شغال / ${b} دقيقة طافي`,autoDesc:(w,m)=>`يطفي لو أقل من ${w} واط لمدة ${m} دقيقة`,timerAt:t=>`الساعة ${t}`,nowPhase:p=>`دلوقتي ${p==='on'?'شغال':'طافي'}`,strip:'المشترك',
  ranges:[['day','24 ساعة'],['week','7 أيام'],['month','30 يوم'],['year','12 شهر']],budget:'الميزانية الشهرية',noBudget:'مفيش ميزانية شهرية. ضيفها من الإعدادات.',ofBudget:(p,b)=>`${p}% من ${b}`,
  estimate:'تقدير من عداد المشترك نفسه، مش فاتورة الكهربا.',at_price:p=>`بسعر ${p} للكيلوواط ساعة`,byOutlet:'حسب الفيشة',
  alerts:'التنبيهات والنشاط',alertsEmpty:'مفيش حاجة حصلت لسه.',
  cost:'التكلفة',price:'سعر الكيلوواط ساعة',currency:'العملة',budgetF:'الميزانية الشهرية (فاضي = مقفولة)',limits:'الحدود',tempMax:'أقصى حرارة للفيشة (°م)',powerMax:'أقصى قدرة إجمالية (واط)',
  push:'إشعارات الموبايل',pushHelp:'ntfy: اشترك في topic طويل وعشوائي من تطبيق ntfy والصق اللينك. تيليجرام: توكن البوت من ‎@BotFather‎ ورقم المحادثة.',
  ntfy:'لينك topic في ntfy',tgToken:'توكن بوت تيليجرام',tgChat:'رقم محادثة تيليجرام',tgSaved:'محفوظ — اكتب عشان تغيّره',clearTg:'امسح التوكن',sendTest:'ابعت تجربة',saved:'اتحفظ',
  sentVia:s=>`اتبعت عن طريق ${s}`,noChannel:'مفيش قناة إشعارات متظبطة',failed:'فشل',appearance:'الشكل',language:'اللغة',theme:'الثيم',themes:{kraft:'ورق كرافت',clean:'أبيض وأزرق',night:'سبورة'},scheme:'فاتح ولا غامق',schemes:{auto:'زي الجهاز',light:'فاتح',dark:'غامق'},
  ev:{offline:'المشترك فصل',online:'المشترك رجع',temp_high:p=>`حرارة عالية: ${p.temp_c} °م`,power_high:p=>`القدرة الإجمالية عالية: ${p.watts} واط`,
   budget:p=>`استهلكت ${p.pct}% من الميزانية الشهرية`,rule_fired:p=>`قاعدة اتنفذت: ${p.on?'تشغيل':'إطفاء'}`,rule_skipped:'قاعدة اتلغت: المشترك مش متصل',rule_failed:'قاعدة اتبعتت بس ما اتأكدتش',autooff:p=>`إطفاء تلقائي عند ${p.watts} واط`},
  alarm:{temp_high:'سخنة',power_high:'حمل عالي'}},
};
const TABS=['plugs','rules','energy','alerts','settings'], TAB_ICONS=['plug','clock','chart','bell','gear'];
const store={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
let lang=store.get('powerk-lang')||((navigator.language||'').startsWith('ar')?'ar':'en');
const THEMES=['kraft','clean','night'];
let theme=THEMES.includes(store.get('powerk-theme'))?store.get('powerk-theme'):'kraft';
let scheme=store.get('powerk-scheme')||'auto';
let tab=TABS.includes(location.hash.slice(1))?location.hash.slice(1):'plugs';
let S=null, range='week', energyMac=null, ruleKind='schedule', pending={};
const t=()=>T[lang];
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';');   // names, model, fw, event text come from outside
async function api(p,method,b){
  const r=await fetch(p,method?{method,headers:method==='DELETE'?{}:{'Content-Type':'application/json'},body:b===undefined?undefined:JSON.stringify(b)}:{cache:'no-store'});
  const j=await r.json().catch(()=>({error:'HTTP '+r.status}));
  if(!r.ok||j.error) throw new Error(j.error||('HTTP '+r.status));
  return j;
}
let toastT;function toast(m){const el=$('toast');el.textContent=m;el.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),3200)}
const run=async f=>{try{await f()}catch(e){toast(e.message)}};
const label=d=>d.label||d.name, oname=o=>o.name||`${t().outlet} ${o.n}`;
const num=v=>(+v).toLocaleString('en-US',{maximumFractionDigits:2});   // Western digits in both languages, like the 30° readings
const money=v=>`${num((+v).toFixed(2))} ${esc(S?S.currency:'')}`;
const loc=()=>lang==='ar'?'ar-EG-u-nu-latn':'en-GB';
const fmtT=s=>s?new Date(s*1000).toLocaleString(loc(),{weekday:'short',hour:'2-digit',minute:'2-digit'}):'';

function setLang(l){lang=l;store.set('powerk-lang',l);document.documentElement.lang=l;document.documentElement.dir=l==='ar'?'rtl':'ltr';chrome();draw(true)}
function setScheme(k){scheme=k;store.set('powerk-scheme',k);if(k==='auto')delete document.documentElement.dataset.scheme;else document.documentElement.dataset.scheme=k;paintBar()}
function setTheme(k){theme=k;store.set('powerk-theme',k);document.documentElement.dataset.theme=k;paintBar()}
function paintBar(){$('themeColor').content=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',paintBar);
function chrome(){
  $('langBtn').textContent=lang==='ar'?'English':'عربي';
  $('nav').innerHTML=TABS.map((k,i)=>`<button ${k===tab?'aria-current=page':''} onclick="go('${k}')">${ic(TAB_ICONS[i])}<span>${t().tabs[i]}</span>${k==='alerts'&&S&&S.unread_alerts?`<span class=badge>${S.unread_alerts}</span>`:''}</button>`).join('');
}
function go(k){tab=k;history.replaceState(null,'','#'+k);chrome();draw(true);scrollTo({top:0})}
async function draw(full){
  if(tab==='plugs') return plugs();
  if(!full) return;
  const f={rules,energy,alerts,settings}[tab];
  try{await f()}catch(e){$('app').innerHTML=`<div class=card><div class=empty>${esc(e.message)}</div></div>`}
}

/* ---------- outlets ---------- */
function plugs(){
  const L=t();
  if(!S){$('app').innerHTML=`<div class=tiles style="margin-top:62px">${'<div class=skeleton></div>'.repeat(4)}</div>`;return}
  if(!S.devices.length){$('app').innerHTML=`<div class=card style="margin-top:8px"><h2>${L.noStrip}</h2><p class=muted style="margin:8px 0 0">${L.noStripBody}</p></div>`;return}
  $('app').innerHTML=S.devices.map(d=>{
    const alarms=(d.alarms||[]).map(a=>`<span class=pill>${ic('warn')}${L.alarm[a]||esc(a)}</span>`).join('');
    const busy=[1,2,3,4].some(n=>(d.mac+':'+n) in pending), dis=!d.online||busy?'disabled':'';
    return `<section class="dev${d.online?'':' offline'}">
    <div class=devhead><h1>${esc(label(d))}</h1>
      <button class=iconbtn aria-label="${L.rename}" onclick="rename('${d.mac}',0)">${ic('pen')}</button>
      <span class="state${d.online?' on':''}">${d.online?L.online:L.offline}</span></div>
    ${alarms?`<div class="row alarms">${alarms}</div>`:''}
    <div class="card sum"><small>${L.now}</small><strong><bdi>${num(d.power_w)}<span>W</span></bdi></strong>
      <div class=mini>${d.voltage?`<span><bdi><b>${num(d.voltage)} V</b> · ${num(d.current_a)} A</bdi></span>`:''}
        <span>${L.today} <b><bdi>${num(d.today_kwh??0)} kWh</bdi></b></span><span>${L.month} <b><bdi>${money(d.month_cost??0)}</bdi></b></span></div></div>
    <div class=tiles>${d.outlets.map(o=>{const key=d.mac+':'+o.n, pend=key in pending, on=pend?pending[key]:o.on;
      const al=(o.alarms||[]).map(a=>L.alarm[a]||a).join(' · ');
      return `<div class="tile${on?' on':''}${pend?' pending':''}">
        <button class=tap aria-pressed=${on} ${d.online&&!pend?'':'disabled'} aria-busy=${pend} onclick="cmd('${d.mac}',${o.n},${!o.on})">
          <span class=tname>${esc(oname(o))}<small>${pend?L.sending:on?L.on:L.off} · <bdi>${o.temp_c}°</bdi>${al?` · <span class=alarm>${esc(al)}</span>`:''}</small></span>
          <span class=tfoot><span class=tw><bdi>${num(o.power_w)}<small>W</small></bdi></span><span class=knob></span></span>
        </button>
        <button class=edit aria-label="${L.rename} ${esc(oname(o))}" onclick="rename('${d.mac}',${o.n})">${ic('pen')}</button></div>`}).join('')}</div>
    <div class=both><button class="btn dark" ${dis} onclick="cmd('${d.mac}',0,true)">${ic('power')}${L.allOn}</button><button class=btn ${dis} onclick="cmd('${d.mac}',0,false)">${L.allOff}</button></div>
  </section>`}).join('');
}
function cmd(mac,outlet,on){
  const keys=outlet?[mac+':'+outlet]:[1,2,3,4].map(n=>mac+':'+n);
  if(keys.some(k=>k in pending)) return;
  keys.forEach(k=>pending[k]=on);plugs();
  run(async()=>{try{const r=await api('api/onoff','POST',{mac,outlet,on});if(r.confirmed===false)toast(t().notConfirmed)}finally{keys.forEach(k=>delete pending[k]);await refresh()}});
}
function rename(mac,n){
  const d=S.devices.find(x=>x.mac===mac);const cur=n?(d.outlets.find(o=>o.n===n).name||''):(d.label||'');
  $('dlgForm').innerHTML=`<h2 style="margin-bottom:16px">${n?t().outletName:t().stripName}</h2>
    <label class=f>${n?`${t().outlet} ${n}`:esc(d.name)}<input id=dlgIn maxlength=40 value="${esc(cur)}" placeholder="${n?`${t().outlet} ${n}`:esc(d.name)}"></label>
    <p class=muted style="margin:-6px 0 18px">${t().resetHint}</p>
    <div class=both><button class="btn plain" value=cancel>${t().cancel}</button><button class="btn primary" value=ok>${t().save}</button></div>`;
  const dlg=$('dlg');dlg.returnValue='';dlg.showModal();$('dlgIn').select();
  dlg.onclose=()=>{if(dlg.returnValue==='ok')run(async()=>{await api('api/name','POST',{mac,outlet:n,name:$('dlgIn').value.trim()});await refresh()})};
}

/* ---------- rules ---------- */
function describe(r,d){
  const L=t(), outs=r.outlets.length===4?L.allOutlets:r.outlets.map(n=>{const o=d&&d.outlets.find(x=>x.n===n);return o?oname(o):`${L.outlet} ${n}`}).join('، ');
  const w=r.on?L.turnOn:L.turnOff;
  if(r.kind==='schedule') return [`${r.at} · ${w}`,`${r.days.length===7?L.everyDay:r.days.map(x=>L.days[x]).join(' ')} · ${outs}`];
  if(r.kind==='timer') return [`${L.kinds.timer} · ${w}`,`${L.timerAt(fmtT(r.fire_at))} · ${outs}`];
  if(r.kind==='cycle') return [L.cycleDesc(r.on_min,r.off_min),`${outs}${r.phase?' · '+L.nowPhase(r.phase):''}`];
  return [L.autoDesc(r.below_w,r.for_min),outs];
}
const RULE_ICON={schedule:'cal',timer:'clock',cycle:'cycle',autooff:'power'};
async function rules(){
  const L=t(), {rules}=await api('api/rules'), devs=S?S.devices:[];
  const list=rules.map(r=>{const d=devs.find(x=>x.mac===r.mac);const [a,b]=describe(r,d);
    return `<div class=item><span class=dotk>${ic(RULE_ICON[r.kind]||'clock')}</span>
      <div class=grow><b>${esc(r.label||a)}</b><span class=muted>${esc(r.label?a+' · ':'')}${esc(b)}${r.enabled&&r.next_at?` · ${L.next} ${fmtT(r.next_at)}`:''}</span></div>
      <button class=switch role=switch aria-checked=${r.enabled} aria-label="${esc(r.label||a)}" onclick="ruleOn('${r.id}',${!r.enabled})"></button>
      <button class=iconbtn aria-label="${L.delRule}" onclick="ruleDel('${r.id}')">${ic('trash')}</button></div>`}).join('');
  const d0=devs[0];
  const outs=`<div class=checks>${[1,2,3,4].map(n=>{const o=d0&&d0.outlets.find(x=>x.n===n);return `<label><input type=checkbox class=rout value=${n} ${n===1?'checked':''}><span>${esc(o?oname(o):L.outlet+' '+n)}</span></label>`}).join('')}</div>`;
  const onoff=`<label class=f>${L.action}<select id=ron><option value=1>${L.turnOn}</option><option value=0>${L.turnOff}</option></select></label>`;
  const fields={
    schedule:`<div class=grid2><label class=f>${L.at}<input id=rat type=time value="07:00"></label>${onoff}</div>
      <h3>${L.repeat}</h3><div class=checks>${L.days.map((x,i)=>`<label><input type=checkbox class=rday value=${i} checked><span>${x}</span></label>`).join('')}</div>`,
    timer:`<div class=grid2><label class=f>${L.inMin}<input id=rmin type=number inputmode=numeric min=1 max=10080 value=30></label>${onoff}</div>`,
    cycle:`<div class=grid2><label class=f>${L.onMin}<input id=ronm type=number inputmode=numeric min=1 max=1440 value=10></label><label class=f>${L.offMin}<input id=roffm type=number inputmode=numeric min=1 max=1440 value=50></label></div>`,
    autooff:`<div class=grid2><label class=f>${L.belowW}<input id=rw type=number inputmode=decimal min=0.1 step=0.1 value=3></label><label class=f>${L.forMin}<input id=rfor type=number inputmode=numeric min=1 max=1440 value=10></label></div>`,
  };
  $('app').innerHTML=`<h1 style="margin:6px 0 14px">${L.rules}</h1>
  <section class=card style="padding-block:6px"><div class=list>${list||`<div class=empty>${L.rulesEmpty}</div>`}</div></section>
  ${devs.length?`<section class=card><h2>${L.newRule}</h2>
    <div class=seg role=group style="margin-bottom:18px">${Object.entries(L.kinds).map(([k,v])=>`<button aria-pressed=${k===ruleKind} onclick="ruleKind='${k}';rules()">${v}</button>`).join('')}</div>
    ${devs.length>1?`<label class=f>${L.strip}<select id=rmac>${devs.map(d=>`<option value="${d.mac}">${esc(label(d))}</option>`).join('')}</select></label>`:''}
    <h3>${L.outlets}</h3>${outs}${fields[ruleKind]}
    <label class=f>${L.label}<input id=rlabel maxlength=40></label>
    <button class="btn primary" style="width:100%" onclick="ruleAdd()">${ic('plus')}${L.addRule}</button></section>`:''}`;
}
function ruleAdd(){run(async()=>{
  const v=id=>$(id)&&$(id).value, n=id=>Number(v(id));
  const r={kind:ruleKind,mac:v('rmac')||S.devices[0].mac,label:v('rlabel').trim(),enabled:true,outlets:[...document.querySelectorAll('.rout:checked')].map(x=>+x.value)};
  if(ruleKind==='schedule'){r.at=v('rat');r.on=v('ron')==='1';r.days=[...document.querySelectorAll('.rday:checked')].map(x=>+x.value)}
  if(ruleKind==='timer'){r.minutes=n('rmin');r.on=v('ron')==='1'}
  if(ruleKind==='cycle'){r.on_min=n('ronm');r.off_min=n('roffm')}
  if(ruleKind==='autooff'){r.below_w=n('rw');r.for_min=n('rfor')}
  await api('api/rules','POST',r);toast(t().added);rules();
})}
const ruleOn=(id,en)=>run(async()=>{await api('api/rules/'+id,'PUT',{enabled:en});rules()});
const ruleDel=id=>{if(confirm(t().delRule))run(async()=>{await api('api/rules/'+id,'DELETE');rules()})};

/* ---------- energy ---------- */
async function energy(){
  const L=t();
  if(!S||!S.devices.length){$('app').innerHTML=`<div class=card style="margin-top:8px"><div class=empty>${L.noStrip}</div></div>`;return}
  energyMac=energyMac||S.devices[0].mac;
  const h=await api(`api/history?mac=${energyMac}&range=${range}`), d=S.devices.find(x=>x.mac===energyMac)||S.devices[0];
  const max=Math.max(...h.buckets.map(b=>b.kwh),0.001);
  const lab=s=>{const x=new Date(s*1000);return h.unit==='hour'?x.toLocaleTimeString(loc(),{hour:'2-digit'}):h.unit==='day'?x.toLocaleDateString(loc(),{day:'numeric',month:'numeric'}):x.toLocaleDateString(loc(),{month:'short'})};
  const spent=S.month_cost??S.devices.reduce((a,x)=>a+(x.month_cost||0),0), pct=S.budget?Math.round(spent/S.budget*100):0;
  $('app').innerHTML=`<h1 style="margin:6px 0 14px">${L.tabs[2]}</h1>
  <section class=card>
    ${S.devices.length>1?`<label class=f>${L.strip}<select onchange="energyMac=this.value;energy()">${S.devices.map(x=>`<option value=${x.mac} ${x.mac===energyMac?'selected':''}>${esc(label(x))}</option>`).join('')}</select></label>`:''}
    <div class=seg role=group>${L.ranges.map(([k,v])=>`<button aria-pressed=${k===range} onclick="range='${k}';energy()">${v}</button>`).join('')}</div>
    <div class=total><bdi>${num(h.total_kwh)}<span>kWh</span></bdi></div>
    <div class=muted><bdi>${money(h.total_cost)}</bdi></div>
    <div class=bars role=img aria-label="${num(h.total_kwh)} kWh">${h.buckets.map(b=>`<div class="${b.kwh?'':'zero'}" title="${lab(b.t)}: ${b.kwh} kWh · ${b.cost}" style="height:${Math.max(b.kwh/max*100,0)}%"></div>`).join('')}</div>
    <div class=axis><span>${lab(h.buckets[0].t)}</span><span>${lab(h.buckets[h.buckets.length-1].t)}</span></div>
  </section>
  <section class=card><h2>${L.byOutlet}</h2>
    <div class=legend style="margin-top:0">${h.per_outlet.map(p=>{const o=d.outlets.find(x=>x.n===p.n);return `<div><span>${esc(o?oname(o):L.outlet+' '+p.n)}</span><b><bdi>${num(p.kwh)} kWh</bdi></b></div>`}).join('')}</div>
  </section>
  <section class=card><h2>${L.month}</h2>
    <div class=total style="margin-top:4px"><bdi>${money(spent)}</bdi></div>
    <div class=muted><bdi>${num(S.month_kwh??0)} kWh</bdi> · ${L.at_price(`<bdi>${S.price_kwh} ${esc(S.currency)}</bdi>`)}</div>
    ${S.budget?`<h3 style="margin:20px 0 0">${L.budget}</h3><div class=meter><div style="width:${Math.min(100,pct)}%;background:${pct>=90?'var(--bad)':pct>=75?'var(--warn)':'var(--ok)'}"></div></div><div class=muted>${L.ofBudget(pct,`<bdi>${money(S.budget)}</bdi>`)}</div>`:`<p class=muted style="margin:14px 0 0">${L.noBudget}</p>`}
    <p class=muted style="margin:14px 0 0">${L.estimate}</p></section>`;
}

/* ---------- alerts ---------- */
async function alerts(){
  const L=t(), {events}=await api('api/events?limit=100');
  if(events.length&&S&&S.unread_alerts){await api('api/events/read','POST',{through_id:events[0].id});S.unread_alerts=0;chrome()}
  const text=e=>{const f=L.ev[e.code];return f?(typeof f==='function'?f(e.params||{}):f):e.text};
  $('app').innerHTML=`<h1 style="margin:6px 0 14px">${L.alerts}</h1><section class=card style="padding-block:6px"><div class=list>${events.map(e=>`
    <div class="item ev ${e.level}"><span class=dotk>${ic(e.level==='info'?'info':'warn')}</span>
    <div class=grow><b>${esc(text(e))}</b><span class=muted>${new Date(e.ts*1000).toLocaleString(loc(),{dateStyle:'medium',timeStyle:'short'})}${e.outlet?` · ${L.outlet} ${e.outlet}`:''}</span></div></div>`).join('')
    ||`<div class=empty>${L.alertsEmpty}</div>`}</div></section>`;
}

/* ---------- settings ---------- */
async function settings(){
  const L=t(), s=await api('api/settings');
  const f=(k,lbl,type='text',extra='')=>`<label class=f>${lbl}<input id=s_${k} type=${type} ${extra} value="${esc(s[k]??'')}"></label>`;
  $('app').innerHTML=`<h1 style="margin:6px 0 14px">${L.tabs[4]}</h1>
  <section class=card><h2>${L.appearance}</h2>
    <h3>${L.language}</h3><div class=seg role=group style="margin-bottom:16px">${[['ar','عربي'],['en','English']].map(([k,v])=>`<button aria-pressed=${k===lang} onclick="setLang('${k}')">${v}</button>`).join('')}</div>
    <h3>${L.theme}</h3><div class=seg role=group>${THEMES.map(k=>`<button aria-pressed=${k===theme} onclick="setTheme('${k}');settings()">${L.themes[k]}</button>`).join('')}</div>
    ${theme==='clean'?`<h3 style="margin-top:16px">${L.scheme}</h3><div class=seg role=group>${Object.entries(L.schemes).map(([k,v])=>`<button aria-pressed=${k===scheme} onclick="setScheme('${k}');settings()">${v}</button>`).join('')}</div>`:''}</section>
  ${(S?S.devices:[]).map(d=>{const lv=d.rssi>=-60?0:d.rssi>=-70?1:d.rssi>=-80?2:3;
    const row=(k,v)=>`<div><span>${k}</span><b>${v}</b></div>`;
    return `<section class=card><h2>${L.stripInfo}${S.devices.length>1?' · '+esc(label(d)):''}</h2><div class=legend style="margin-top:0">
      ${row(L.conn,`<span style="color:var(${d.online?'--ok':'--bad'})">${d.online?L.online:L.offline}</span>`)}
      ${d.rssi?row(L.wifi,`<span style="color:var(${['--ok','--ok','--warn','--bad'][lv]})">${L.wifiLv[lv]}</span> <bdi class=muted>(${d.rssi} dBm)</bdi>`):''}
      ${d.ip?row(L.ipAddr,`<bdi>${esc(d.ip)}</bdi>`):''}${row(L.model,`<bdi>${esc(d.model)||'—'}</bdi>`)}
      ${row(L.fw,`<bdi>${esc(d.fw)||'—'}</bdi>`)}${row(L.mac,`<bdi>${esc(d.mac)}</bdi>`)}</div>
      ${d.rssi?`<p class=muted style="margin:10px 0 0">${L.wifiHelp}</p>`:''}</section>`}).join('')}
  <section class=card><h2>${L.cost}</h2>
    <div class=grid2>${f('price_kwh',L.price,'number','step=0.01 min=0 inputmode=decimal')}${f('currency',L.currency)}</div>${f('budget',L.budgetF,'number','step=1 min=0 inputmode=numeric')}</section>
  <section class=card><h2>${L.limits}</h2>
    <div class=grid2>${f('temp_max_c',L.tempMax,'number','min=30 max=120 inputmode=numeric')}${f('power_max_w',L.powerMax,'number','min=100 max=10000 inputmode=numeric')}</div></section>
  <section class=card><h2 style="margin-bottom:6px">${L.push}</h2><p class=muted style="margin:0 0 16px">${L.pushHelp}</p>
    ${f('ntfy_url',L.ntfy,'url','dir=ltr placeholder="https://ntfy.sh/…"')}
    <div class=grid2>${f('telegram_token',L.tgToken,'password',`dir=ltr autocomplete=off placeholder="${s.telegram_configured?L.tgSaved:''}"`)}${f('telegram_chat',L.tgChat,'text','dir=ltr inputmode=numeric')}</div>
    ${s.telegram_configured?`<button class="btn danger" style="margin-bottom:14px" onclick="run(async()=>{await api('api/settings','PUT',{telegram_token:''});toast(t().saved);settings()})">${ic('trash')}${L.clearTg}</button>`:''}
    <button class="btn plain" style="width:100%" onclick="testPush()">${ic('send')}${L.sendTest}</button></section>
  <button class="btn primary" style="width:100%" onclick="saveSettings()">${L.save}</button>`;
}
function saveSettings(){run(async()=>{
  const g=k=>$('s_'+k).value.trim(), n=k=>g(k)===''?null:Number(g(k));
  const body={currency:g('currency'),price_kwh:n('price_kwh'),budget:n('budget'),temp_max_c:n('temp_max_c'),power_max_w:n('power_max_w'),ntfy_url:g('ntfy_url'),telegram_chat:g('telegram_chat')};
  if(g('telegram_token')) body.telegram_token=g('telegram_token');      // empty field keeps the saved token
  await api('api/settings','PUT',body);toast(t().saved);await refresh();settings();
})}
function testPush(){run(async()=>{
  const r=await fetch('api/alerts/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(x=>x.json());
  const L=t();toast(r.sent&&r.sent.length?L.sentVia(r.sent.join(', '))+(r.failed.length?` · ${r.failed.map(f=>f.channel+' '+L.failed).join(', ')}`:''):r.failed&&r.failed.length?r.failed.map(f=>`${f.channel}: ${f.error}`).join(' · '):L.noChannel);
})}

async function refresh(){
  try{S=await api('api/state');chrome();draw(false)}catch(e){toast(e.message)}
}
setTheme(theme);setScheme(scheme);setLang(lang);
refresh().then(()=>draw(true));setInterval(()=>{if(!document.hidden)refresh()},2000);
</script>
"""


# Web fonts for PAGE, served at /fonts/<name>. Subsets of Atkinson Hyperlegible (+ Mono), Kalam,
# Permanent Marker, Marhey and IBM Plex Sans Arabic from Google Fonts; all under the SIL Open Font
# License 1.1. Bundled here so the page looks right on a LAN with no internet access.
FONT_FILES = {
    "AtkinsonHyperlegible-400-latin.woff2": "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",
    "AtkinsonHyperlegible-700-latin.woff2": "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",
    "AtkinsonHyperlegibleMono-400-latin.woff2": "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",
    "IBMPlexSansArabic-400-arabic.woff2": "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",
    "IBMPlexSansArabic-600-arabic.woff2": "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",
    "Kalam-700-latin.woff2": "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",
    "Marhey-700-arabic.woff2": "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",
    "Marhey-700-latin.woff2": "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",
    "PermanentMarker-400-latin.woff2": "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",
}


class Web(BaseHTTPRequestHandler):
    hub: Hub
    engine: Engine
    loop: asyncio.AbstractEventLoop
    local_ip: str
    port: int
    token: str = ""

    def log_message(self, fmt, *args):
        pass

    def _authorized(self) -> bool:
        """No token set -> open. Otherwise: X-Token header, HTTP Basic (browser), ?t= (simple clients)
        or the cookie the page sets after a ?t= visit (so the page's own API calls work too)."""
        if not self.token:
            return True
        if secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        if "powerk_t" in cookie and secrets.compare_digest(unquote(cookie["powerk_t"].value), self.token):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            if secrets.compare_digest(password, self.token) or secrets.compare_digest(user, self.token):
                return True
        return secrets.compare_digest(parse_qs(urlparse(self.path).query).get("t", [""])[0], self.token)

    def _deny(self):
        api = urlparse(self.path).path.startswith("/api/")
        body = b'{"error": "token required"}' if api else b"powerk: token required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="powerk"')
        self.send_header("Content-Type", "application/json" if api else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, body: bytes, ctype: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """JSON body or None (error already sent). JSON only: a cross-site form/no-cors POST cannot
        send this type without a CORS preflight, so another web page cannot drive the API (CSRF)."""
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._json({"error": "Content-Type must be application/json"}, 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 16384:
                raise ValueError("body too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body
        except Exception:
            self._json({"error": "bad request"}, 400)
            return None

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _run(self, coro, timeout: float = 10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    # -- GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/powerk.py", "/powerk.apk") and not self._authorized():
            self._deny()
            return
        if path == "/api/state":
            snap = self.hub.snapshot()
            snap["local_ip"], snap["port"] = self.local_ip, self.port
            self._json(snap)
        elif path == "/api/rules":
            mac = self._query().get("mac", "").upper()
            now = time.time()
            self._json({"rules": [self.engine.view(r, now) for r in self.hub.store.rules() if not mac or r["mac"] == mac]})
        elif path == "/api/history":
            q = self._query()
            try:
                outlet = int(q["outlet"]) if q.get("outlet") else None
                if outlet is not None and outlet not in (1, 2, 3, 4):
                    raise ValueError("outlet must be 1-4")
                mac = q.get("mac", "").upper()
                if mac not in self.hub.devices:
                    raise ValueError("mac must be a known strip")
                s = self.hub.store.settings()
                h = self.hub.db.history(mac, q.get("range", "week"), s["price_kwh"], outlet)
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            h["currency"] = s["currency"]
            self._json(h)
        elif path == "/api/settings":
            self._json(public_settings(self.hub.store.settings()))
        elif path == "/api/events":
            q = self._query()
            try:
                limit = max(1, min(500, int(q.get("limit", 50))))
                before = int(q["before_id"]) if q.get("before_id") else None
            except ValueError:
                self._json({"error": "limit and before_id must be numbers"}, 400)
                return
            self._json({"events": self.hub.db.events(limit, before),
                        "unread_alerts": self.hub.db.unread(self.hub.store.meta("events_read_id", 0))})
        elif path == "/api/debug":
            self._json({"devices": [{"mac": d.mac, "online": d.online, "last_getinfo": d.last_getinfo,
                                     "counter_wh": d.counter_wh} for d in list(self.hub.devices.values())]})
        elif path.startswith("/fonts/") and path[7:] in FONT_FILES:
            body = base64.b64decode(FONT_FILES[path[7:]])
            self.send_response(200)
            self.send_header("Content-Type", "font/woff2")
            self.send_header("Cache-Control", "max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if self.token:
                self.send_header("Set-Cookie", f"powerk_t={quote(self.token)}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/powerk.py":               # public: lets a phone/borrowed laptop fetch this file to run `provision`
            self._file(Path(__file__).resolve().read_bytes(), "text/x-python", "powerk.py")
        elif path == "/powerk.apk":              # public: the Android app, once built (android/ -> gradlew assembleDebug)
            apk = Path(__file__).resolve().parent / "android/app/build/outputs/apk/debug/app-debug.apk"
            if not apk.is_file():
                self._json({"error": "no APK built yet: cd android && gradlew assembleDebug"}, 404)
                return
            self._file(apk.read_bytes(), "application/vnd.android.package-archive", "powerk.apk")
        else:
            self._json({"error": "not found"}, 404)

    # -- POST / PUT / DELETE
    def do_POST(self):
        self._write("POST")

    def do_PUT(self):
        self._write("PUT")

    def do_DELETE(self):
        self._write("DELETE")

    def _write(self, method: str):
        path = urlparse(self.path).path
        routes = {("POST", "/api/onoff"), ("POST", "/api/name"), ("POST", "/api/rules"), ("PUT", "/api/settings"),
                  ("POST", "/api/alerts/test"), ("POST", "/api/events/read")}
        rule_id = path[len("/api/rules/"):] if path.startswith("/api/rules/") else None
        if (method, path) not in routes and not (rule_id and method in ("PUT", "DELETE")):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._deny()
            return
        body = {} if method == "DELETE" else self._body()
        if body is None:
            return
        store = self.hub.store

        if path == "/api/onoff":
            self._onoff(body)
        elif path == "/api/name":
            mac = str(body.get("mac", "")).upper()
            outlet, name = body.get("outlet"), body.get("name", "")
            if (mac not in self.hub.devices or not _is_outlet(outlet, 0)
                    or not isinstance(name, str) or len(name.strip()) > NAME_MAX):
                self._json({"error": f"need a known mac, outlet 0-4 and a name up to {NAME_MAX} characters"}, 400)
                return
            store.set_name(mac, outlet, name.strip())
            self._json({"ok": True})
        elif path == "/api/rules":
            try:
                rule = clean_rule(body, time.time())
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            store.put_rule(rule)
            self._json({"ok": True, "rule": self.engine.view(rule)})
        elif rule_id:
            old = store.rule(rule_id)
            if not old:
                self._json({"error": "no such rule"}, 404)
                return
            if method == "DELETE":
                store.delete_rule(rule_id)
                self.engine.reset(rule_id)
                self._json({"ok": True})
                return
            try:
                rule = clean_rule(body, time.time(), base=old)
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            store.put_rule(rule)
            self.engine.reset(rule_id)                    # e.g. a cycle restarts with its "on" phase
            self._json({"ok": True, "rule": self.engine.view(rule)})
        elif path == "/api/settings":
            try:
                changes = clean_settings(body)
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            self._json(public_settings(store.update_settings(changes)))
        elif path == "/api/alerts/test":
            sent, failed = self.hub.notifier.test()
            reply = {"ok": bool(sent) and not failed, "sent": sent, "failed": failed}
            if not sent and not failed:
                reply["error"] = "no push channel configured"
            self._json(reply)
        elif path == "/api/events/read":
            last = self.hub.db.last_event_id()
            through = body.get("through_id", last)
            if type(through) is not int or through < 0:
                self._json({"error": "through_id must be a whole number"}, 400)
                return
            read = store.raise_meta("events_read_id", min(through, last))
            self._json({"ok": True, "unread_alerts": self.hub.db.unread(read)})

    def _onoff(self, req: dict):
        try:
            mac = str(req["mac"]).upper()
            outlet = req["outlet"]
            on = req["on"]
            if not isinstance(on, bool) or not _is_outlet(outlet, 0):   # bool("false") is True, int(0.9) is "all": never guess
                raise ValueError("on must be true/false, outlet a whole number 0-4")
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        s = self.hub.session(mac)
        if not s:
            self._json({"error": "device offline"}, 503)
            return
        channels = [1, 2, 3, 4] if outlet == 0 else [outlet]
        try:
            confirmed = self._run(s.set_outlets(channels, on))
        except Exception as err:
            self._json({"error": f"command sent, outcome unknown: {err!r}"}, 502)
            return
        # ok = commands sent; confirmed = a fresh snapshot shows every requested outlet in the wanted state
        self._json({"ok": True, "confirmed": bool(confirmed)})


def public_settings(s: dict) -> dict:
    """Settings for the API: the bot token is write-only."""
    s = dict(s)
    s["telegram_configured"] = bool(s.pop("telegram_token"))
    return s


def clean_settings(body: dict) -> dict:
    out = {}
    for key, value in body.items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting {key!r}")
        if key == "currency":
            if not isinstance(value, str) or not 0 < len(value.strip()) <= 8:
                raise ValueError("currency must be 1-8 characters")
            value = value.strip()
        elif key in ("price_kwh", "budget", "temp_max_c", "power_max_w"):
            if value is None and key == "budget":
                pass
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a number")
            else:
                lo, hi = {"price_kwh": (0, 1000), "budget": (0.01, 10**7), "temp_max_c": (30, 120), "power_max_w": (100, 10000)}[key]
                if not lo <= value <= hi:
                    raise ValueError(f"{key} must be {lo}-{hi}")
        elif key == "ntfy_url":
            if not isinstance(value, str) or (value and urlparse(value).scheme not in ("http", "https")):
                raise ValueError("ntfy_url must be an http(s) URL or empty")
        elif key == "telegram_token":            # omitted = keep, "" = clear, text = replace
            if not isinstance(value, str):
                raise ValueError("telegram_token must be text")
        elif key == "telegram_chat":
            if not isinstance(value, str):
                raise ValueError("telegram_chat must be text")
        out[key] = value.strip() if isinstance(value, str) else value
    return out


# ---------------------------------------------------------------- commands

def local_ipv4s() -> list[str]:
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        ips = []
    return [i for i in ips if not i.startswith(("127.", "169.254."))]


def setup_command(cmd: str, expect: str, host: str, timeout: float = 6.0) -> str:
    with socket.create_connection((host, SETUP_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((cmd + "\r\n").encode())
        buf = b""
        deadline = time.monotonic() + timeout
        while b"\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = s.recv(1024)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    text = buf.decode("utf-8", "replace").strip()
    if expect and expect not in text:
        raise SystemExit(f"strip answered {text!r}, expected {expect!r} (command {cmd!r})")
    return text


def cmd_provision(args) -> int:
    for value in (args.ssid, args.password):
        if any(c in value for c in ":\r\n"):
            raise SystemExit("the strip's command protocol cannot use ':' or newlines in the SSID/password")

    if not args.ip:
        print("Need the IP this PC will have on your home Wi-Fi (the strip connects to it).")
        cands = local_ipv4s()
        print("Candidates found right now:", ", ".join(cands) or "none")
        print("Run `python powerk.py` on the home Wi-Fi first; it prints the exact IP. Then:")
        print('  python powerk.py provision --ip 192.168.0.50 --ssid "YOURWIFI" --password "..."')
        return 2

    try:
        args.ip = str(ipaddress.IPv4Address(args.ip))     # never pass an unchecked string into the strip's command protocol
    except ValueError:
        raise SystemExit(f"--ip must be an IPv4 address (got {args.ip!r})")

    print(f"Looking for the strip's setup service at {args.host}:{SETUP_PORT} ...")
    deadline = time.monotonic() + args.wait
    while True:
        try:
            with socket.create_connection((args.host, SETUP_PORT), timeout=2):
                break
        except OSError:
            if time.monotonic() > deadline:
                print("Not reachable. Do this first:")
                print("  1. hold the strip's main button ~10s until the LED blinks fast")
                print("  2. join the Wi-Fi network 'TONLY_TAP_XXXXXXX' from this PC")
                print("     (password: LGU_XXXXXXX - the same 7 characters as in the name)")
                print("  3. re-run this command")
                return 1
            time.sleep(2)

    print(f"  {setup_command(f'up:ip:{args.ip}', 'up:ip:ip_ok', args.host)}")
    print(f"  {setup_command(f'up:connect:{args.ssid}:{args.password}', 'up:connect:connect_ok', args.host)}")
    print(f"\nDone. Reconnect this PC to your home Wi-Fi and start `python powerk.py`.")
    print(f"The strip will connect to {args.ip}:{DEVICE_PORT} shortly.")
    return 0


def parse_macs(values: list[str]) -> set[str]:
    macs = {m.strip().upper().replace(":", "").replace("-", "") for v in values for m in v.split(",") if m.strip()}
    bad = [m for m in macs if not MAC_RE.match(m)]
    if bad:
        raise SystemExit(f"--mac needs 12 hex digits, got {', '.join(bad)}")
    return macs


async def cmd_serve(args) -> int:
    data = Path(args.data).resolve() if args.data else Path(__file__).resolve().parent
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "powerk.json")
    hub = Hub(store, Db(str(data / "powerk.db")), allow=parse_macs(args.mac))
    hub.restore()
    engine = Engine(hub)
    server = await asyncio.start_server(hub.handle_client, "0.0.0.0", args.port)
    candidates = local_ipv4s()
    ip = args.ip or (candidates or ["127.0.0.1"])[0]

    Web.hub, Web.engine, Web.loop, Web.local_ip, Web.port = hub, engine, asyncio.get_running_loop(), ip, args.port
    Web.token = args.token
    httpd = ThreadingHTTPServer(("0.0.0.0", args.web_port), Web)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(f"powerk")
    print(f"  web UI (phone/PC)  http://{ip}:{args.web_port}")
    print(f"  strip server       TCP {args.port}")
    print(f"  auth               {'token required' if args.token else 'NONE - only safe on a trusted LAN'}")
    if args.token:
        print(f"                     browser: user 'powerk' + the token as password; app: Settings -> Token")
    print(f"  strips allowed     {', '.join(sorted(hub.allow)) if hub.allow else 'any (use --mac on a public server)'}")
    print(f"  data               {data}")
    print(f"  if not provisioned yet:  python powerk.py provision --ip {ip} --ssid WIFI --password PW")
    if (Path(__file__).resolve().parent / "android/app/build/outputs/apk/debug/app-debug.apk").is_file():
        print(f"  android app        http://{ip}:{args.web_port}/powerk.apk")
    if len(candidates) > 1 and not args.ip:
        print(f"  other local IPs: {', '.join(c for c in candidates if c != ip)}  (pick one with --ip if wrong)")
    print("  Ctrl+C to stop")
    async with server:
        await asyncio.gather(hub.poll_loop(), engine.loop())
    return 0


# ---------------------------------------------------------------- selftest

def _fake_tags(state_on: set[int], energy: dict[int, int] | None = None) -> str:
    """A 'up:getinfo:' payload shaped like the real strip's, for offline checks."""
    energy = energy or {}
    return ":".join(
        f"{n}:{n * 111};{'on' if n in state_on else 'off'};{int(n in state_on)};0;0;"
        f"{5300 if n == 2 and n in state_on else 0};{energy.get(n, 10):08X};00000000;00000000;0;00;{30 + n}"
        for n in (1, 2, 3, 4)
    )


async def _selftest() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="powerk-selftest-"))
    store = Store(tmp / "powerk.json")
    sent: list[str] = []
    hub = Hub(store, Db(str(tmp / "powerk.db")))
    hub.notifier.send = lambda _title, text: sent.append(text)
    engine = Engine(hub)
    server = await asyncio.start_server(hub.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    # A stand-in for the strip: answers getinfo, records onoff commands, keeps relay state.
    state_on = {2}
    energy = {1: 10, 2: 10, 3: 10, 4: 10}

    async def fake_strip():
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode().strip()
            if line == "up:getinfo:all":
                writer.write(f"up:getinfo:{_fake_tags(state_on, energy)}\r\n".encode())
                await writer.drain()
            elif line.startswith("up:onoff:"):
                _, _, ch, val = line.split(":")
                (state_on.add if val == "on" else state_on.discard)(int(ch))

    strip_task = asyncio.create_task(fake_strip())

    writer.write(b"up:bootinfo:LGU+-TAP-HW002;aabbccddeeff;001122334455;1.0.66;connect\r\n")
    writer.write(b"up:power_report:1:224500\r\n")
    writer.write(b"up:query:-52\r\n")
    await writer.drain()
    await asyncio.sleep(0.3)

    dev = hub.devices["AABBCCDDEEFF"]
    sess = hub.session("aabbccddeeff")
    assert dev.outlets[1]["on"] is False and dev.outlets[2]["on"] is True
    assert dev.outlets[2]["power_w"] == 5.3 and dev.outlets[2]["energy_kwh"] == 0.01
    assert dev.outlets[1]["temp_c"] == 31 and dev.outlets[4]["temp_c"] == 34
    assert dev.voltage_v == 224.5 and dev.rssi == -52
    assert dev.current_a == 0.02

    assert await sess.set_outlets([2], False) is True
    assert dev.outlets[2]["on"] is False and state_on == set()
    snapshot = hub.snapshot()["devices"][0]
    assert snapshot["on"] is False and snapshot["energy_kwh"] == 0.04

    writer.write(b"up:event:onoff:0:on\r\n")        # master button switches all four
    await writer.drain()
    await asyncio.sleep(0.2)
    assert all(o["on"] for o in dev.outlets.values())

    # energy: counter deltas go to the hourly table; a counter reset counts its new value
    energy[1] = 510
    dev.counter_ts -= 1800                          # 500 Wh in 30 min is plausible (1 kW)
    await sess.refresh()
    energy[1] = 20
    await sess.refresh()
    assert hub.db._sum_wh("AABBCCDDEEFF", 0) == 520, "500 Wh delta + 20 Wh after reset"
    h = hub.db.history("AABBCCDDEEFF", "day", 2.0)
    assert len(h["buckets"]) == 24 and h["total_kwh"] == 0.52 and h["total_cost"] == 1.04
    assert h["per_outlet"][0] == {"n": 1, "kwh": 0.52}
    for rng, count in (("week", 7), ("month", 30), ("year", 12)):
        assert len(hub.db.history("AABBCCDDEEFF", rng, 2.0)["buckets"]) == count

    # names
    store.set_name("AABBCCDDEEFF", 0, "Desk")
    store.set_name("AABBCCDDEEFF", 3, "Fridge")
    snap = hub.snapshot()["devices"][0]
    assert snap["label"] == "Desk" and snap["outlets"][2]["name"] == "Fridge" and snap["outlets"][0]["name"] == ""
    assert Store(tmp / "powerk.json").name("AABBCCDDEEFF", 3) == "Fridge", "names persist"

    # rule validation
    now = time.time()
    for bad in ({"kind": "schedule", "mac": "AABBCCDDEEFF", "outlets": [1], "at": "25:00", "days": [0], "on": True},
                {"kind": "timer", "mac": "AABBCCDDEEFF", "outlets": [1], "minutes": 0, "on": True},
                {"kind": "cycle", "mac": "AABBCCDDEEFF", "outlets": [9], "on_min": 1, "off_min": 1},
                {"kind": "autooff", "mac": "nope", "outlets": [1], "below_w": 3, "for_min": 1},
                {"kind": "schedule", "mac": "AABBCCDDEEFF", "outlets": [1], "at": "07:00", "days": [0], "on": "false"}):
        try:
            clean_rule(bad, now)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass

    for bad in ({"kind": "cycle", "mac": "AABBCCDDEEFF", "outlets": [1.0], "on_min": 1, "off_min": 1},
                {"kind": "timer", "mac": "AABBCCDDEEFF", "outlets": [1], "minutes": float("inf"), "on": True},
                {"kind": "autooff", "mac": "AABBCCDDEEFF", "outlets": [1], "below_w": float("nan"), "for_min": 1}):
        try:
            clean_rule(bad, now)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    t1 = clean_rule({"kind": "timer", "mac": "AABBCCDDEEFF", "outlets": [1], "minutes": 5, "on": True, "fire_at": 1}, now)
    assert t1["fire_at"] == round(now + 300), "a client cannot set fire_at"
    store.put_rule(t1)
    assert store.consume_rule(dict(t1, on=False)) is False and store.rule(t1["id"]), "an edited timer is not consumed"
    store.delete_rule(t1["id"])

    # schedule fires once in its minute, on the right weekday
    base = datetime.now().replace(hour=7, minute=30, second=5, microsecond=0).timestamp()
    wd = datetime.fromtimestamp(base).weekday()
    sched = clean_rule({"kind": "schedule", "mac": "AABBCCDDEEFF", "outlets": [0], "at": "07:30", "days": [wd], "on": False}, base)
    store.put_rule(sched)
    await engine.tick(base)
    assert state_on == set(), "schedule switched all off"
    state_on.add(1)
    await engine.tick(base + 20)
    assert state_on == {1}, "same minute does not fire twice"
    assert engine.view(sched, base + 60)["next_at"] == int(base - 5 + 7 * 86400), "next run is next week"
    store.delete_rule(sched["id"])

    # timer fires once, then deletes itself
    timer = clean_rule({"kind": "timer", "mac": "AABBCCDDEEFF", "outlets": [3], "minutes": 5, "on": True}, now)
    store.put_rule(timer)
    await engine.tick(now + 60)
    assert 3 not in state_on
    await engine.tick(now + 301)
    assert 3 in state_on and store.rule(timer["id"]) is None

    # cycle: on, then off after on_min, then on after off_min
    state_on.clear()
    await sess.refresh()
    cyc = clean_rule({"kind": "cycle", "mac": "AABBCCDDEEFF", "outlets": [4], "on_min": 2, "off_min": 3}, now)
    store.put_rule(cyc)
    await engine.tick(now)
    assert 4 in state_on and engine.view(cyc, now)["phase"] == "on"
    await engine.tick(now + 119)
    assert 4 in state_on
    await engine.tick(now + 121)
    assert 4 not in state_on and engine.view(cyc, now)["phase"] == "off"
    await engine.tick(now + 121 + 181)
    assert 4 in state_on
    store.delete_rule(cyc["id"])
    engine.reset(cyc["id"])

    # auto-off: outlet 2 draws 5.3 W; below 10 W for 1 min -> off
    state_on.clear()
    state_on.add(2)
    auto = clean_rule({"kind": "autooff", "mac": "AABBCCDDEEFF", "outlets": [2], "below_w": 10, "for_min": 1}, now)
    store.put_rule(auto)
    await sess.refresh()                            # the reading starts the low-draw countdown
    engine.rt[auto["id"]]["below_since"][2] = now   # pin it to the fake clock
    await engine.tick(now)
    dev.reported_at = now + 30                      # the engine runs on a fake clock; keep readings "fresh"
    await engine.tick(now + 30)
    assert 2 in state_on
    dev.reported_at = now + 61
    await engine.tick(now + 61)
    assert 2 not in state_on and any("Auto-off" in t for t in sent)
    state_on.add(2)
    await sess.refresh()
    assert 2 in engine.rt[auto["id"]]["below_since"]
    dev.outlets[2] = dict(dev.outlets[2], power_w=50.0)
    engine.observe(dev, now)                        # one high reading cancels the countdown
    assert 2 not in engine.rt[auto["id"]]["below_since"]
    store.delete_rule(auto["id"])
    state_on.discard(2)
    await sess.refresh()

    # alarms are edge-triggered: one alert per raise
    store.update_settings({"temp_max_c": 34})        # only outlet 4 (34 C) is at the limit
    await sess.refresh()
    await sess.refresh()
    assert sum("temp high" in t for t in sent) == 1, sent
    assert hub.snapshot()["devices"][0]["outlets"][3]["alarm"] == "temp_high"

    # budget: 0.52 kWh * 100 = 52 -> 50 % of 100
    store.update_settings({"price_kwh": 100.0, "budget": 100.0})
    hub.db._totals.clear()
    engine._budget(now)
    engine._budget(now)
    assert sum("budget 50%" in t for t in sent) == 1
    assert hub.db.events(5)[0]["code"] == "budget"

    # settings validation
    for bad in ({"price_kwh": "x"}, {"budget": -1}, {"ntfy_url": "ftp://x"}, {"nope": 1}):
        try:
            clean_settings(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert "telegram_token" not in public_settings(store.settings())
    store.update_settings(clean_settings({"telegram_token": "123:abc"}))
    assert public_settings(store.settings())["telegram_configured"] is True

    # events: newest first, ids, unread counts only warn/alert
    evs = hub.db.events(50)
    assert evs[0]["id"] > evs[-1]["id"] and all(isinstance(e["ts"], int) for e in evs)
    assert hub.db.unread(0) == sum(e["level"] != "info" for e in evs) > 0
    assert hub.db.unread(hub.db.last_event_id()) == 0
    assert [e["id"] for e in hub.db.events(2, before_id=evs[0]["id"])] == [evs[1]["id"], evs[2]["id"]]

    # known strips come back (offline) after a restart
    hub2 = Hub(Store(tmp / "powerk.json"), Db(":memory:"))
    hub2.restore()
    assert "AABBCCDDEEFF" in hub2.devices and hub2.devices["AABBCCDDEEFF"].online is False

    # an offline gap spreads the counter delta over the hours it covers
    db = Db(":memory:")
    t = datetime(2026, 1, 5, 12, 30).timestamp()
    db.add_energy("M", 1, 300.0, t, since=t - 3 * 3600)
    assert len(db.conn.execute("SELECT * FROM energy").fetchall()) == 4 and db._sum_wh("M", 0) == 300.0
    db2 = Db(":memory:")
    t = datetime(2026, 2, 1, 0, 0).timestamp()
    db2.add_energy("M", 1, 300.0, t, since=t - 3 * 3600)    # [21:00, 00:00): nothing lands in February
    assert db2._sum_wh("M", t) == 0 and db2._sum_wh("M", 0) == 300.0

    # a one-reading counter glitch keeps the baseline; a jump that persists becomes the new baseline
    g = Device("AABBCCDDEEFF", "m", "f", "")
    recs = lambda wh: [dict(n=n, on=True, power_w=1.0, energy_kwh=wh / 1000 if n == 1 else 0.0, temp_c=30) for n in (1, 2, 3, 4)]
    hub3 = Hub(Store(None), Db(":memory:"))
    for i, wh in enumerate((10, 10000, 11)):
        hub3.on_getinfo(g, recs(wh), 1.79e9 + 5 * i)
    assert hub3.db._sum_wh(None, 0) == 1, "10 -> spike -> 11 counts 1 Wh"
    for i, wh in enumerate((90000, 90000, 90000, 90001)):
        hub3.on_getinfo(g, recs(wh), 1.79e9 + 100 + 5 * i)
    assert hub3.db._sum_wh(None, 0) == 2 and g.counter_wh[1] == 90001

    # allowlist: an unknown strip is refused
    hub.allow = {"AABBCCDDEEFF"}
    r2, w2 = await asyncio.open_connection("127.0.0.1", port)
    w2.write(b"up:bootinfo:x;112233445566;112233445566;1;connect\r\n")
    await w2.drain()
    assert await r2.read() == b"" and "112233445566" not in hub.devices
    w2.close()

    strip_task.cancel()
    writer.close()
    server.close()
    hub.db.conn.close()
    # every font the page asks for is bundled, and every bundled font is a woff2 the page uses
    wanted = set(re.findall(r"url\(fonts/([\w.-]+)\)", PAGE))
    assert wanted == set(FONT_FILES), wanted ^ set(FONT_FILES)
    assert all(base64.b64decode(v)[:4] == b"wOF2" for v in FONT_FILES.values())

    print("selftest: parser, commands, energy, names, rules, alarms, budget, allowlist, fonts OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run device server + web UI (default)")
    s.add_argument("--port", type=int, default=DEVICE_PORT, help=f"device TCP port (default {DEVICE_PORT}, the strip's fixed port)")
    s.add_argument("--web-port", type=int, default=WEB_PORT)
    s.add_argument("--ip", help="IP to print in the UI/provision hint (default: auto-detect)")
    s.add_argument("--token", default="", help="require this token (password) for the web UI/API; use it whenever the port is reachable from the internet")
    s.add_argument("--mac", action="append", default=[], help="only accept these strip MACs (comma separated or repeated); use on a public server")
    s.add_argument("--data", help="directory for powerk.json and powerk.db (default: next to powerk.py)")
    p = sub.add_parser("provision", help="send home Wi-Fi + this PC's IP to a strip in setup mode")
    p.add_argument("--ip", help="IPv4 this PC will have on the home Wi-Fi (what the strip connects to)")
    p.add_argument("--ssid", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--host", default=SETUP_HOST, help=f"strip setup address (default {SETUP_HOST})")
    p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the strip setup service")
    sub.add_parser("selftest", help="offline protocol + rules + energy check")
    args = ap.parse_args()

    if args.cmd == "provision":
        return cmd_provision(args)
    if args.cmd == "selftest":
        asyncio.run(_selftest())
        return 0
    if args.cmd in (None, "serve"):
        for key, default in (("port", DEVICE_PORT), ("web_port", WEB_PORT), ("ip", None), ("token", ""), ("mac", []), ("data", None)):
            setattr(args, key, getattr(args, key, default))
        try:
            return asyncio.run(cmd_serve(args))
        except KeyboardInterrupt:
            return 0
    ap.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
