#!/usr/bin/env python3
"""powerk - local control for the LG U+ / Jinheung MTTL-W01 4-outlet smart strip.

Stdlib only, one file, three commands:

  python powerk.py serve       TCP 10086 device server + web UI on 8080  (default)
  python powerk.py provision   point the strip at this PC (while in SoftAP setup mode)
  python powerk.py selftest    offline protocol check

The strip speaks plain text lines ("up:...") over TCP 10086. It connects to this
PC directly because provisioning stores the PC's IP in the strip. No cloud, no
router DNS or DNAT change, no MQTT, no Home Assistant.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import io
import ipaddress
import json
import math
import queue
import re
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEVICE_PORT = 10086
SETUP_HOST = "192.168.1.1"      # strip SoftAP setup service
SETUP_PORT = 30300
WEB_PORT = 8080
POLL_SECONDS = 5.0
DIAG_EVERY = 3                  # voltage/current/RSSI every 3rd poll
COMMAND_TIMEOUT = 8.0
CLIENT_TIMEOUT = 20.0
MAX_BODY = 4096
HISTORY_INTERVAL = 30.0
HISTORY_DB = Path(__file__).resolve().parent / "data" / "history.sqlite3"

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


def parse_getinfo(line: str) -> list[dict]:
    """Decode confirmed AC channels 1..4; do not interpret extra records as USB data."""
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
        })
    return out


class History:
    """Durable per-outlet samples. A bounded writer queue keeps disk I/O off TCP handling."""

    CSV_FIELDS = ("timestamp_utc", "unix_seconds", "mac", "outlet", "relay_on", "power_w",
                  "energy_counter_wh", "temperature_c", "voltage_v", "rssi_dbm",
                  "previous_timestamp_utc", "interval_seconds", "energy_delta_wh", "quality")

    def __init__(self, path: str | Path, interval: float = HISTORY_INTERVAL):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.interval = interval
        self.pending = queue.Queue(maxsize=128)
        self.worker: threading.Thread | None = None
        self.write_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.accepting = False
        with closing(self.connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported history database version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history_devices (
                    mac TEXT PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL,
                    firmware TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS readings (
                    recorded_at REAL NOT NULL, mac TEXT NOT NULL, outlet INTEGER NOT NULL,
                    relay_on INTEGER NOT NULL, power_w REAL NOT NULL, counter_wh INTEGER NOT NULL,
                    temperature_c INTEGER NOT NULL, voltage_v REAL, rssi_dbm INTEGER,
                    previous_at REAL, interval_seconds REAL, delta_wh INTEGER,
                    quality TEXT NOT NULL CHECK(quality IN ('baseline','ok','gap','reset')),
                    PRIMARY KEY(mac, outlet, recorded_at)
                );
                CREATE INDEX IF NOT EXISTS readings_time ON readings(recorded_at);
                PRAGMA user_version=1;
            """)
            last, count = db.execute("SELECT MAX(recorded_at), COUNT(*) FROM readings").fetchone()
        self._status = {"enabled": True, "interval_seconds": interval,
                        "last_recorded_at": last, "readings_saved": count,
                        "failed_snapshots": 0, "error": None}

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def status(self) -> dict:
        with self.status_lock:
            return dict(self._status)

    def start(self) -> None:
        self.accepting = True
        self.worker = threading.Thread(target=self._write_loop, name="powerk-history", daemon=True)
        self.worker.start()

    def _failed(self, message: str) -> None:
        with self.status_lock:
            self._status = {**self._status, "error": message,
                            "failed_snapshots": self._status["failed_snapshots"] + 1}

    def submit(self, snapshot: dict) -> None:
        if not self.accepting:
            return
        try:
            self.pending.put_nowait(snapshot)
        except queue.Full:
            self._failed("History queue full; some readings were not saved")

    def _write_loop(self) -> None:
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                self.record(item)
            except Exception as error:
                self._failed("History write failed; check the server log")
                print(f"[history] write failed: {error!r}")
            finally:
                self.pending.task_done()

    def close(self) -> None:
        self.accepting = False
        if self.worker:
            self.pending.put(None)
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise RuntimeError("history writer did not finish flushing queued readings")
            self.worker = None

    def record(self, sample: dict) -> bool:
        """Save only fresh, complete snapshots; first samples and resets never add usage."""
        if not sample.get("online"):
            return False
        at = sample["last_seen"]
        outlets = sample["outlets"]
        if not isinstance(at, (float, int)) or not math.isfinite(at) or at < 0:
            raise ValueError("invalid sample timestamp")
        if len(outlets) != 4 or {o["n"] for o in outlets} != {1, 2, 3, 4}:
            raise ValueError("history requires four distinct outlets")
        mac = sample["mac"]
        with self.write_lock, closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = {o["n"]: db.execute(
                "SELECT recorded_at, counter_wh FROM readings WHERE mac=? AND outlet=? "
                "ORDER BY recorded_at DESC LIMIT 1", (mac, o["n"])).fetchone() for o in outlets}
            latest = max((row["recorded_at"] for row in previous.values() if row), default=None)
            counters = {o["n"]: int(round(o["energy_kwh"] * 1000)) for o in outlets}
            if any(value < 0 for value in counters.values()):
                raise ValueError("negative energy counter")
            reset = any(row and counters[n] < row["counter_wh"] for n, row in previous.items())
            if latest is not None and (at <= latest or (at - latest < self.interval and not reset)):
                return False
            rows = []
            for outlet in outlets:
                prev = previous[outlet["n"]]
                elapsed = at - prev["recorded_at"] if prev else None
                delta = counters[outlet["n"]] - prev["counter_wh"] if prev else None
                if prev is None:
                    quality = "baseline"
                elif delta < 0:
                    quality = "reset"
                    delta = None
                elif elapsed > max(120, self.interval * 3):
                    quality = "gap"
                else:
                    quality = "ok"
                rows.append((at, mac, outlet["n"], int(outlet["on"]), outlet["power_w"],
                             counters[outlet["n"]], outlet["temp_c"], sample.get("voltage"),
                             sample.get("rssi"), prev["recorded_at"] if prev else None,
                             elapsed, delta, quality))
            db.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.execute("INSERT INTO history_devices VALUES (?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET "
                       "name=excluded.name, model=excluded.model, firmware=excluded.firmware, last_seen=excluded.last_seen",
                       (mac, sample["name"], sample["model"], sample["fw"], at, at))
        with self.status_lock:
            self._status = {**self._status, "last_recorded_at": max(at, self._status["last_recorded_at"] or at),
                            "readings_saved": self._status["readings_saved"] + 4, "error": None}
        return True

    @staticmethod
    def parameters(query: str) -> dict:
        values = parse_qs(query, keep_blank_values=True, max_num_fields=12)
        allowed = {"start", "end", "mac", "outlet", "bucket", "t"}
        if set(values) - allowed or any(len(value) != 1 for value in values.values()):
            raise ValueError("unknown or repeated history parameter")
        now = time.time()
        start = float(values.get("start", [now - 7 * 86400])[0])
        end = float(values.get("end", [now])[0])
        if not all(math.isfinite(t) for t in (start, end)) or start < 0 or not start < end or end - start > 366 * 86400:
            raise ValueError("use a start/end range of at most 366 days (Unix seconds)")
        mac = values.get("mac", [""])[0].upper()
        if mac and not re.fullmatch(r"[0-9A-F]{12}", mac):
            raise ValueError("invalid history device")
        outlet = values.get("outlet", [""])[0]
        if outlet not in ("", "1", "2", "3", "4"):
            raise ValueError("history outlet must be 1..4")
        bucket = values.get("bucket", ["day"])[0]
        if bucket not in ("hour", "day"):
            raise ValueError("history bucket must be hour or day")
        return {"start": start, "end": end, "mac": mac,
                "outlet": int(outlet) if outlet else None, "bucket": bucket}

    @staticmethod
    def where(params: dict):
        clause = "recorded_at >= ? AND recorded_at < ?"
        args = [params["start"], params["end"]]
        for key in ("mac", "outlet"):
            if params[key]:
                clause += f" AND {key}=?"
                args.append(params[key])
        return clause, args

    def report(self, params: dict) -> dict:
        clause, args = self.where(params)
        bucket_seconds = 3600 if params["bucket"] == "hour" else 86400
        # A delta belongs to the selected period only if both observations are inside it.
        valid = "CASE WHEN previous_at >= ? THEN COALESCE(delta_wh,0) ELSE 0 END"
        with closing(self.connect()) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            summary = dict(db.execute(f"SELECT COUNT(*) AS readings, SUM({valid}) / 1000.0 AS energy_kwh, "
                "SUM(quality='reset') AS resets, SUM(quality='gap') AS gaps, "
                "MIN(recorded_at) AS first_sample, MAX(recorded_at) AS last_sample "
                f"FROM readings WHERE {clause}", [params["start"], *args]).fetchone())
            buckets = [dict(row) for row in db.execute(
                f"SELECT CAST(recorded_at / ? AS INTEGER) * ? AS timestamp, COUNT(*) AS readings, "
                f"SUM(CASE WHEN previous_at >= CAST(recorded_at / ? AS INTEGER) * ? AND previous_at >= ? "
                "THEN COALESCE(delta_wh,0) ELSE 0 END) / 1000.0 AS energy_kwh, "
                "SUM(quality='reset') AS resets, SUM(quality='gap') AS gaps "
                f"FROM readings WHERE {clause} GROUP BY timestamp ORDER BY timestamp",
                [bucket_seconds, bucket_seconds, bucket_seconds, bucket_seconds, params["start"], *args])]
            devices = [dict(row) for row in db.execute("SELECT * FROM history_devices ORDER BY name, mac")]
        for key in ("energy_kwh", "resets", "gaps"):
            summary[key] = summary[key] or 0
        summary["unallocated_energy_kwh"] = round(summary["energy_kwh"] - sum(b["energy_kwh"] for b in buckets), 6)
        return {"measurement_scope": "ac_outlets_1_to_4",
                "start": params["start"], "end": params["end"], "bucket": params["bucket"],
                "timezone": "UTC", "summary": summary, "buckets": buckets, "devices": devices}

    @staticmethod
    def iso(timestamp) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else ""

    def csv_chunks(self, params: dict):
        """Stream a consistent raw export without buffering it or blocking the writer."""
        clause, args = self.where(params)
        with closing(self.connect()) as db:
            db.execute("PRAGMA query_only=ON")
            cursor = db.execute(f"SELECT * FROM readings WHERE {clause} ORDER BY recorded_at, mac, outlet", args)
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(self.CSV_FIELDS)
            yield buffer.getvalue().encode("utf-8")
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    return
                buffer.seek(0)
                buffer.truncate(0)
                for row in rows:
                    writer.writerow((self.iso(row["recorded_at"]), row["recorded_at"], row["mac"],
                        row["outlet"], row["relay_on"], row["power_w"], row["counter_wh"],
                        row["temperature_c"], row["voltage_v"], row["rssi_dbm"],
                        self.iso(row["previous_at"]), row["interval_seconds"], row["delta_wh"], row["quality"]))
                yield buffer.getvalue().encode("utf-8")


class Device:
    def __init__(self, mac: str, model: str, fw: str, ip: str):
        self.mac = mac.upper()
        self.model = model
        self.fw = fw
        self.ip = ip
        self.online = False
        self.last_seen: float | None = None
        self.voltage_v: float | None = None
        self.rssi: int | None = None
        self.outlets = {n: {"n": n, "on": False, "power_w": 0.0, "energy_kwh": 0.0, "temp_c": 0} for n in range(1, 5)}

    @property
    def name(self) -> str:
        return f"MTTL {self.mac[-7:]}"

    @property
    def current_a(self) -> float:
        if not self.voltage_v:
            return 0.0
        return round(sum(o["power_w"] for o in self.outlets.values()) / self.voltage_v, 2)

    def snapshot(self) -> dict:
        outlets = [dict(outlet) for outlet in self.outlets.values()]
        # Capability metadata for the verified stock family/firmware, not live USB telemetry.
        usb = None
        if self.model.lower() == "lgutap" and self.fw == "0.1.54-1.0.66":
            usb = {"port_count": 2, "power_design": "always_powered",
                   "control": "not_exposed", "metering": "not_reported",
                   "on": None, "power_w": None, "energy_kwh": None}
        return {
            "mac": self.mac,
            "name": self.name,
            "model": self.model,
            "fw": self.fw,
            "ip": self.ip,
            "online": self.online,
            "last_seen": self.last_seen,
            "on": any(o["on"] for o in outlets),
            "power_w": round(sum(o["power_w"] for o in outlets), 2),
            "energy_kwh": round(sum(o["energy_kwh"] for o in outlets), 3),
            "voltage": self.voltage_v,
            "current_a": self.current_a,
            "rssi": self.rssi,
            "outlets": outlets,
            "measurement_scope": "ac_outlets_1_to_4",
            "usb": usb,
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
        async with self.lock:
            for ch in channels:
                await self.send(f"up:onoff:{ch}:{'on' if on else 'off'}")
            await asyncio.sleep(0.3)          # let the relay settle before the snapshot
            return await self._refresh() and all(self.device.outlets[ch]["on"] == on for ch in channels)

    async def diagnostics(self) -> None:
        async with self.lock:
            await self.send("up:power_report:1:vol")
            await self.send("up:query:wifirssi")


class Hub:
    def __init__(self, allowed_devices: set[str] | None = None, history: History | None = None) -> None:
        self.devices: dict[str, Device] = {}
        self.sessions: dict[str, Session] = {}
        self.allowed_devices = allowed_devices or set()
        self.connections = 0
        self.history = history

    def session(self, mac: str) -> Session | None:
        s = self.sessions.get(mac.upper())
        if s and s.device and s.device.online and not s.writer.is_closing():
            return s
        return None

    def snapshot(self) -> dict:
        return {"devices": [d.snapshot() for d in self.devices.values()],
                "history": self.history.status() if self.history else {"enabled": False}}

    async def state(self) -> dict:
        # HTTP workers must read state on the event loop that owns it.
        return self.snapshot()

    async def command(self, mac: str, channels: list[int], on: bool) -> bool:
        session = self.session(mac)
        if session is None:
            raise ConnectionError("device offline")
        return await asyncio.wait_for(session.set_outlets(channels, on), COMMAND_TIMEOUT)

    async def refresh_session(self, session: Session) -> None:
        try:
            if not await session.refresh():
                session.device.online = False
                session.writer.close()
        except (ConnectionError, OSError):
            session.writer.close()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connections >= 32:
            writer.close()
            await writer.wait_closed()
            return
        self.connections += 1
        initial_refresh = None
        handshake_deadline = asyncio.get_running_loop().time() + 10.0
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        s = Session(ip, reader, writer)
        print(f"[device] connection from {ip}")
        try:
            while True:
                timeout = CLIENT_TIMEOUT if s.device else max(0.001, handshake_deadline - asyncio.get_running_loop().time())
                raw = await asyncio.wait_for(reader.readline(), timeout)
                if not raw:
                    break
                if len(raw) > 8192:
                    raise ValueError("line too long")
                line = raw.replace(b"\x00", b"").decode("utf-8", "replace").strip("\r\n ")
                s.last_rx = line

                m = BOOTINFO_RE.match(line)
                if m:
                    mac = m.group(2).upper()
                    # MACs are an enrollment filter, not proof of device identity.
                    if (s.device or (self.allowed_devices and mac not in self.allowed_devices)
                            or self.sessions.get(mac) or (mac not in self.devices and len(self.devices) >= 64)):
                        break
                    d = self.devices.get(mac) or Device(mac, m.group(1), m.group(4), ip)
                    d.model, d.fw, d.ip, d.online = m.group(1), m.group(4), ip, False
                    s.device = d
                    self.devices[mac] = d
                    self.sessions[mac] = s
                    print(f"[device] {d.name} model={d.model} fw={d.fw}")
                    initial_refresh = asyncio.create_task(self.refresh_session(s))
                    continue

                if not s.device:
                    continue

                if line.startswith("up:getinfo:"):
                    records = parse_getinfo(line)
                    if len(records) == 4 and {r["n"] for r in records} == {1, 2, 3, 4}:
                        s.device.outlets = {r["n"]: r for r in records}
                        s.device.online = True
                        s.device.last_seen = time.time()
                        s.got_getinfo.set()
                        if self.history:
                            self.history.submit(s.device.snapshot())
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
                    if ch:
                        s.device.outlets[ch]["on"] = on
                    continue

                if ONOFF_ACK_RE.fullmatch(line):
                    continue
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as err:
            print(f"[device] session error {ip}: {err!r} (last sent {s.last_cmd!r}, last received {s.last_rx!r})")
        finally:
            self.connections -= 1
            if initial_refresh:
                initial_refresh.cancel()
                await asyncio.gather(initial_refresh, return_exceptions=True)
            if s.device:
                if self.sessions.get(s.device.mac) is s:
                    self.sessions.pop(s.device.mac, None)
                    s.device.online = False
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
            async def poll(s):
                if not s.device:
                    return
                try:
                    if not await s.refresh():
                        print(f"[device] {s.device.name} poll timeout")
                        s.device.online = False
                        s.writer.close()
                    elif n % DIAG_EVERY == 0:
                        await s.diagnostics()
                except Exception as err:
                    print(f"[device] poll error: {err!r}")
                    s.device.online = False
                    s.writer.close()
            await asyncio.gather(*(poll(s) for s in list(self.sessions.values())))
            await asyncio.sleep(POLL_SECONDS)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>powerk · Power overview</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --surface: #ffffff;
  --subtle: #f1f3f6;
  --ink: #17202e;
  --muted: #566276;
  --line: #d6dbe3;
  --control-border: #788496;
  --accent: #2457d6;
  --accent-soft: #edf2ff;
  --accent-ink: #2448a5;
  --danger: #b42318;
  --danger-bg: #fff1f0;
  --danger-border: #d92d20;
  --thumb: #ffffff;
  --shadow: 0 2px 8px #17202e08;
  --thumb-shadow: 0 1px 2px #17202e20;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-16: 64px;
  --text-xs: 12px;
  --text-sm: 14px;
  --text-base: 16px;
  --text-lg: 20px;
  --text-xl: 24px;
  --text-2xl: 28px;
  --text-3xl: 32px;
  --text-4xl: 40px;
  --text-metric: 48px;
  --weight-regular: 400;
  --weight-medium: 500;
  --weight-semibold: 600;
  --weight-bold: 700;
  --leading-normal: 1.5;
  --leading-heading: 1.2;
  --leading-tight: 1.1;
  --tracking-tight: -.03em;
  --tracking-label: .08em;
  --radius-control: 8px;
  --radius-card: 12px;
  --radius-panel: 16px;
  --radius-pill: 999px;
  --control-size: 44px;
  --control-wide: 52px;
  --duration-fast: 160ms;
  --content-width: 1120px;
  --header-size: 88px;
}
:root[data-theme=dark] {
  color-scheme: dark;
  --bg: #111318;
  --surface: #191d25;
  --subtle: #202631;
  --ink: #edf1f7;
  --muted: #a9b4c4;
  --line: #343c4a;
  --control-border: #8190a6;
  --accent: #8aacff;
  --accent-soft: #202e4f;
  --accent-ink: #bed0ff;
  --danger: #ffb4ac;
  --danger-bg: #3b2224;
  --danger-border: #ff8b80;
  --thumb: #111318;
  --shadow: none;
  --thumb-shadow: none;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:var(--text-sm);line-height:var(--leading-normal)}
button,summary,a{-webkit-tap-highlight-color:transparent}
button{font:inherit;cursor:pointer;border:1px solid var(--control-border);border-radius:var(--radius-control);background:var(--surface);color:var(--ink);padding:var(--space-2) var(--space-4);min-height:var(--control-size);min-width:var(--control-size);transition:background var(--duration-fast),border-color var(--duration-fast)}
button:hover:not(:disabled){background:var(--subtle);border-color:var(--accent)}
button:disabled{cursor:default;opacity:.5}
button:focus-visible,summary:focus-visible,a:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:3px solid var(--accent);outline-offset:4px}
[hidden]{display:none!important}
.shell{max-width:var(--content-width);margin:0 auto;padding:0 var(--space-8)}
.topbar{min-height:var(--header-size);display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);gap:var(--space-4)}
.brand{display:flex;align-items:center;gap:var(--space-3);font-size:var(--text-xl);font-weight:var(--weight-bold);letter-spacing:var(--tracking-tight)}
.brand-mark{display:grid;place-items:center;width:var(--space-8);height:var(--space-8);border-radius:var(--radius-control);background:var(--ink);color:var(--surface);font-size:var(--text-xl);font-weight:var(--weight-regular);letter-spacing:0}
.top-actions,.status,.actions{display:flex;align-items:center;gap:var(--space-3);flex-wrap:wrap}
.top-actions button{font-size:var(--text-sm);padding:var(--space-2) var(--space-3)}
.status{font-size:var(--text-xs);font-weight:var(--weight-medium);color:var(--muted);gap:var(--space-2)}
.status::before{content:"";width:8px;height:8px;background:var(--muted);border-radius:50%}
.status.live::before{background:var(--accent)}
main{padding:var(--space-10) 0 var(--space-12)}
.eyebrow{font-size:var(--text-xs);font-weight:var(--weight-semibold);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--muted);margin:0 0 var(--space-2)}
h1{font-size:var(--text-3xl);letter-spacing:var(--tracking-tight);line-height:var(--leading-heading);margin:0;font-weight:var(--weight-semibold)}
.intro{display:flex;justify-content:space-between;align-items:end;gap:var(--space-4);margin-bottom:var(--space-6)}
.description{color:var(--muted);margin:var(--space-2) 0 0;font-size:var(--text-sm)}
.updated{font-size:var(--text-xs);color:var(--muted);white-space:nowrap}
.overview{display:grid;grid-template-columns:1.45fr 1fr 1fr;gap:var(--space-4);margin-bottom:var(--space-8)}
.metric{padding:var(--space-6);border:1px solid var(--line);background:var(--surface);border-radius:var(--radius-panel);position:relative;overflow:hidden}
.metric.primary{background:var(--surface);color:var(--ink)}
.metric.primary .metric-value{color:var(--accent)}
.metric-label{font-size:var(--text-sm);font-weight:var(--weight-medium);margin:0;padding-right:var(--space-8)}
.metric-value{display:flex;align-items:baseline;flex-wrap:wrap;font-size:var(--text-metric);letter-spacing:var(--tracking-tight);font-weight:var(--weight-semibold);line-height:var(--leading-tight);margin:var(--space-5) 0 var(--space-2);font-variant-numeric:tabular-nums}
.metric-value>span{min-width:0;overflow-wrap:anywhere;max-width:100%}
.metric-value small{font-size:var(--text-base);letter-spacing:0;font-weight:var(--weight-regular);margin-left:var(--space-2);color:var(--muted)}
.metric-note{font-size:var(--text-xs);color:var(--muted);margin:0}
.metric-icon{position:absolute;top:var(--space-6);right:var(--space-6);font-size:var(--text-xl);line-height:1;color:var(--muted)}
.section-heading{display:flex;align-items:center;justify-content:space-between;gap:var(--space-3);margin-bottom:var(--space-4)}
h2{font-size:var(--text-lg);letter-spacing:var(--tracking-tight);font-weight:var(--weight-semibold);margin:0}
.count{color:var(--muted);font-size:var(--text-xs);background:var(--surface);border:1px solid var(--line);padding:var(--space-1) var(--space-3);border-radius:var(--radius-pill)}
#error{border:1px solid var(--danger-border);background:var(--danger-bg);color:var(--danger);border-radius:var(--radius-control);padding:var(--space-3) var(--space-4);margin-bottom:var(--space-6);font-size:var(--text-sm);overflow-wrap:anywhere}
#error:empty{display:none}
#error[data-kind=success]{background:var(--subtle);border-color:var(--control-border);color:var(--ink)}
#error[data-kind=pending]{background:var(--subtle);border-color:var(--control-border);color:var(--muted)}
.strip{border:1px solid var(--line);border-radius:var(--radius-panel);background:var(--surface);box-shadow:var(--shadow);padding:var(--space-6);margin-bottom:var(--space-5);min-width:0}
.strip-header{display:flex;justify-content:space-between;align-items:center;gap:var(--space-5);flex-wrap:wrap}
.strip-heading{display:flex;gap:var(--space-3);align-items:center;min-width:0}
.strip-icon{width:var(--control-size);height:var(--control-size);display:grid;place-items:center;background:var(--subtle);border:1px solid var(--line);border-radius:var(--radius-card);font-size:var(--text-xl);color:var(--muted);flex-shrink:0}
.strip h3{font-size:var(--text-base);letter-spacing:var(--tracking-tight);margin:0;overflow-wrap:anywhere}
.strip-caption{font-size:var(--text-xs);color:var(--muted);margin-top:var(--space-1)}
.pill{font-size:var(--text-xs);font-weight:var(--weight-medium);padding:var(--space-1) var(--space-2);border-radius:var(--radius-pill);background:var(--subtle);color:var(--ink)}
.pill.offline{background:var(--subtle);color:var(--muted)}
.actions button{font-size:var(--text-sm);min-height:var(--control-size);padding:var(--space-2) var(--space-3)}
.strip-summary{display:flex;gap:var(--space-4);flex-wrap:wrap;font-size:var(--text-xs);color:var(--muted);margin:var(--space-5) 0 var(--space-4)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--space-3)}
.outlet{border:1px solid var(--line);background:var(--subtle);border-radius:var(--radius-card);padding:var(--space-4);min-width:0;position:relative}
.outlet.is-on{background:var(--surface);border-color:var(--control-border)}
.outlet.is-on .outlet-state{color:var(--accent)}
.outlet-top{display:flex;justify-content:space-between;align-items:center;gap:var(--space-2);margin-bottom:var(--space-6)}
.socket{width:40px;height:40px;border:1.5px solid var(--muted);border-radius:50%;position:relative;flex-shrink:0}
.socket::before,.socket::after{content:"";position:absolute;background:var(--muted);width:4px;height:8px;top:14px;border-radius:2px}
.socket::before{left:12px}.socket::after{right:12px}
.toggle{position:relative;min-height:var(--control-size);width:var(--control-wide);padding:0;border:0;background:transparent;flex-shrink:0}
.toggle-track{display:block;height:24px;width:44px;border-radius:var(--radius-pill);background:var(--control-border);position:absolute;left:4px;top:10px}
.toggle-track::after{content:"";position:absolute;top:4px;left:4px;width:16px;height:16px;border-radius:50%;background:var(--thumb);box-shadow:var(--thumb-shadow);transition:transform var(--duration-fast)}
.toggle[aria-checked=true] .toggle-track{background:var(--accent)}
.toggle[aria-checked=true] .toggle-track::after{transform:translateX(20px)}
.toggle:hover:not(:disabled){background:transparent;border:0}
.outlet-label{font-size:var(--text-sm);font-weight:var(--weight-semibold);margin:0}
.outlet-state{font-size:var(--text-xs);color:var(--muted);margin:var(--space-1) 0 var(--space-4);min-height:var(--space-5)}
.outlet-power{font-size:var(--text-2xl);letter-spacing:var(--tracking-tight);font-weight:var(--weight-medium);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.outlet-power small{font-size:var(--text-xs);letter-spacing:0;margin-left:var(--space-1);color:var(--muted)}
.outlet-detail{font-size:var(--text-xs);color:var(--muted);margin-top:var(--space-1)}
.device-details{border-top:1px solid var(--line);margin-top:var(--space-5);padding-top:var(--space-3);font-size:var(--text-xs);color:var(--muted)}
summary{cursor:pointer;min-height:var(--control-size);align-content:center;font-weight:var(--weight-medium)}
.device-detail-text{overflow-wrap:anywhere;margin:var(--space-2) 0 0;line-height:var(--leading-normal)}
.usb-info{margin-top:var(--space-4);padding:var(--space-4);background:var(--subtle);border:1px solid var(--line);border-radius:var(--radius-control)}
.usb-info h4{margin:0;font-size:var(--text-sm);font-weight:var(--weight-semibold)}
.usb-info p{margin:var(--space-2) 0 0;font-size:var(--text-xs);color:var(--muted)}
.empty{border:1px dashed var(--line);border-radius:var(--radius-panel);text-align:center;padding:var(--space-12) var(--space-6);background:var(--surface)}
.empty-symbol{font-size:var(--text-3xl);color:var(--muted);margin-bottom:var(--space-3)}
.empty h3{font-size:var(--text-lg);letter-spacing:var(--tracking-tight);margin:0 0 var(--space-2)}
.empty p{color:var(--muted);font-size:var(--text-sm);max-width:370px;margin:0 auto var(--space-5)}
.setup{margin-top:var(--space-6);border-top:1px solid var(--line);padding-top:var(--space-5);color:var(--muted);font-size:var(--text-sm)}
.setup summary{font-size:var(--text-sm)}.setup p{max-width:650px}.setup code{display:block;background:var(--surface);border:1px solid var(--line);padding:var(--space-4);border-radius:var(--radius-control);overflow-wrap:anywhere;white-space:pre-wrap;color:var(--ink);font-size:var(--text-xs)}
footer{display:flex;justify-content:space-between;gap:var(--space-3);flex-wrap:wrap;color:var(--muted);font-size:var(--text-xs);margin-top:var(--space-8)}
.history{margin-top:var(--space-8);padding:var(--space-6);background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-panel);min-width:0}
.history-filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:end;gap:var(--space-3);margin:var(--space-5) 0}
.history-filters label{display:grid;gap:var(--space-1);font-size:var(--text-xs);color:var(--muted);min-width:0}
select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--control-border);border-radius:var(--radius-control);min-height:var(--control-size);min-width:0;max-width:100%;padding:var(--space-2)}
.history-filter-actions{grid-column:1/-1;display:flex;flex-wrap:wrap;justify-content:flex-end;gap:var(--space-3)}
@supports (appearance:base-select){
  @media (hover:hover) and (pointer:fine){
    .history select,.history select::picker(select){appearance:base-select}
    .history select{width:100%;font-size:var(--text-sm);cursor:pointer}
    .history select>button{all:unset;display:flex;align-items:center;flex:1;min-width:0}
    .history selectedcontent{display:grid;min-width:0;text-align:left}
    .history .option-label{font-weight:var(--weight-semibold);overflow-wrap:anywhere}
    .history .option-detail{display:block;font-size:var(--text-xs);color:var(--muted);overflow-wrap:anywhere}
    .history .option-separator{display:none}
    .history select::picker(select){max-width:calc(100vw - var(--space-8));max-height:60vh;overflow:auto;padding:var(--space-1);border:1px solid var(--control-border);border-radius:var(--radius-control);background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}
    .history option{min-height:var(--control-size);padding:var(--space-2) var(--space-3);border-radius:var(--radius-control);gap:var(--space-2);white-space:normal}
    .history option:hover,.history option:focus-visible{background:var(--subtle)}
    .history option:checked{background:var(--accent-soft);color:var(--accent-ink)}
    .history option:checked .option-detail{color:var(--accent-ink)}
    .history option::checkmark{color:var(--accent);margin-left:auto;order:1}
    .history select::picker-icon{color:var(--muted);margin-left:var(--space-2)}
  }
}
.history-stats{display:flex;flex-wrap:wrap;gap:var(--space-6);padding:var(--space-4) 0;border-top:1px solid var(--line)}
.history-stat strong{display:block;font-size:var(--text-xl);font-weight:var(--weight-semibold);font-variant-numeric:tabular-nums}
.history-stat span,.history-message,.history-note{font-size:var(--text-xs);color:var(--muted)}
.history-message[data-kind=error]{color:var(--danger)}
.history-table-wrap{overflow-x:auto;max-width:100%}
.history table{border-collapse:collapse;width:100%;min-width:480px;font-size:var(--text-xs);text-align:left;font-variant-numeric:tabular-nums}
.history th,.history td{padding:var(--space-3);border-bottom:1px solid var(--line)}
.history th{font-weight:var(--weight-semibold);color:var(--muted)}
.history-note{margin:var(--space-3) 0 0}
@media(max-width:900px){.history-filters{grid-template-columns:minmax(0,1fr)}.history-device{grid-column:1/-1;grid-row:1}}
@media(max-width:760px){.history{padding:var(--space-5)}}
@media(max-width:380px){.history{padding:var(--space-3)}}
@media(max-width:760px){.shell{padding:0 var(--space-5)}.topbar{min-height:80px}.status{display:none}.overview{grid-template-columns:1fr 1fr;gap:var(--space-3)}.primary{grid-column:1/-1}.metric{padding:var(--space-5)}.metric-value{font-size:var(--text-3xl);margin-top:var(--space-4)}.metric-icon{top:var(--space-5);right:var(--space-5)}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.strip{padding:var(--space-5)}.intro{align-items:start;flex-direction:column;gap:var(--space-3)}main{padding-top:var(--space-8)}.outlet-top{margin-bottom:var(--space-4)}}
@media(max-width:380px){.shell{padding:0 var(--space-3)}.top-actions{gap:var(--space-2)}.top-actions button{padding:var(--space-2);font-size:var(--text-xs)}.brand{font-size:var(--text-lg);gap:var(--space-2)}.strip{padding:var(--space-3)}.grid{gap:var(--space-2)}.outlet{padding:var(--space-3)}.socket{width:32px;height:32px}.socket::before,.socket::after{top:10px}.socket::before{left:8px}.socket::after{right:8px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important}}
</style>
</head>
<body>
<div class="shell">
<header class="topbar">
  <div class="brand"><span class="brand-mark" aria-hidden="true">⏻</span>powerk</div>
  <div class="top-actions"><span class="status" id="connection">Connecting</span><button id="theme" type="button" aria-pressed="false">Dark theme</button><button id="refresh" type="button">↻ Refresh</button></div>
</header>
<main>
  <div class="intro"><div><p class="eyebrow">Device dashboard</p><h1>Power overview</h1><p class="description">Monitor usage and control each outlet.</p></div><span class="updated" id="updated">Waiting for a live update</span></div>
  <div id="error" role="status" aria-live="polite"></div>
  <section class="overview" aria-label="Power summary">
    <div class="metric primary"><span class="metric-icon" aria-hidden="true">ϟ</span><p class="metric-label">AC outlet power</p><div class="metric-value"><span id="total-power">—</span><small>W</small></div><p class="metric-note" id="power-note">Waiting for connected strips</p></div>
    <div class="metric"><span class="metric-icon" aria-hidden="true">⏻</span><p class="metric-label">Active AC outlets</p><div class="metric-value"><span id="active-outlets">—</span><small id="outlet-total"></small></div><p class="metric-note">Across connected strips</p></div>
    <div class="metric"><span class="metric-icon" aria-hidden="true">↗</span><p class="metric-label">AC energy counter</p><div class="metric-value"><span id="total-energy">—</span><small>kWh</small></div><p class="metric-note" id="energy-note">Device-reported cumulative total</p></div>
  </section>
  <div class="section-heading"><h2>Your power strips</h2><span class="count" id="device-count">Connecting…</span></div>
  <div id="app"></div>
  <div class="empty" id="empty"><div class="empty-symbol" aria-hidden="true">⌁</div><h3 id="empty-title">Connecting to your home</h3><p id="empty-body">Your strips and their latest readings will appear here.</p><button id="setup-button" type="button">View connection setup</button></div>
  <section class="history" aria-labelledby="history-title">
    <div class="section-heading"><h2 id="history-title">AC consumption history</h2><span class="pill" id="history-recording">Checking recorder…</span></div>
    <p class="history-note" id="history-saved">Recording begins when the server receives a complete strip reading.</p>
    <div class="history-filters">
      <label>Period<select id="history-range"><button type="button"><selectedcontent></selectedcontent></button><option value="1"><span class="option-copy"><span class="option-label">Last 24 hours</span><span class="option-separator"> — </span><span class="option-detail">Hourly UTC buckets</span></span></option><option value="7" selected><span class="option-copy"><span class="option-label">Last 7 days</span><span class="option-separator"> — </span><span class="option-detail">Daily UTC buckets</span></span></option><option value="30"><span class="option-copy"><span class="option-label">Last 30 days</span><span class="option-separator"> — </span><span class="option-detail">Daily UTC buckets</span></span></option></select></label>
      <label class="history-device">Power strip<select id="history-device"><button type="button"><selectedcontent></selectedcontent></button><option value=""><span class="option-copy"><span class="option-label">All strips</span><span class="option-separator"> — </span><span class="option-detail">Combined saved history</span></span></option></select></label>
      <label>AC outlet<select id="history-outlet"><button type="button"><selectedcontent></selectedcontent></button><option value=""><span class="option-copy"><span class="option-label">All AC outlets</span><span class="option-separator"> — </span><span class="option-detail">Combined outlets 1–4</span></span></option><option value="1"><span class="option-copy"><span class="option-label">Outlet 1</span><span class="option-separator"> — </span><span class="option-detail">AC socket 1 only</span></span></option><option value="2"><span class="option-copy"><span class="option-label">Outlet 2</span><span class="option-separator"> — </span><span class="option-detail">AC socket 2 only</span></span></option><option value="3"><span class="option-copy"><span class="option-label">Outlet 3</span><span class="option-separator"> — </span><span class="option-detail">AC socket 3 only</span></span></option><option value="4"><span class="option-copy"><span class="option-label">Outlet 4</span><span class="option-separator"> — </span><span class="option-detail">AC socket 4 only</span></span></option></select></label>
      <div class="history-filter-actions"><button id="history-refresh" type="button">Refresh history</button><button id="history-export" type="button" disabled>Export CSV</button></div>
    </div>
    <p class="history-message" id="history-message" role="status" aria-live="polite">Waiting for saved readings.</p>
    <div class="history-stats">
      <div class="history-stat"><strong id="history-energy">—</strong><span>Metered increase (kWh)</span></div>
      <div class="history-stat"><strong id="history-readings">—</strong><span>Saved outlet readings</span></div>
      <div class="history-stat"><strong id="history-flags">—</strong><span>Flagged intervals</span></div>
    </div>
    <div class="history-table-wrap" tabindex="0" aria-label="Consumption history table; scroll horizontally on small screens">
      <table><thead><tr><th scope="col">Period (UTC)</th><th scope="col">Increase (kWh)</th><th scope="col">Readings</th><th scope="col">Flags</th></tr></thead><tbody id="history-rows"></tbody></table>
    </div>
    <p class="history-note" id="history-boundaries" hidden></p>
    <p class="history-note">AC outlets 1–4 only; USB charging is not separately reported. Only complete observed intervals are counted. First readings establish a baseline; counter resets and long gaps are flagged. CSV contains raw measurements for later analysis.</p>
  </section>
  <details class="setup" id="setup"><summary>Connection &amp; setup</summary><p id="hint">Waiting for server details.</p><p>To connect a new strip, provision it with this server's address while connected to the strip's setup Wi-Fi.</p><code id="provision">python powerk.py provision --ip SERVER_IP --ssid YOUR_WIFI --password YOUR_PASSWORD</code><p>Keep the strip connection on a trusted network. Refresh the dashboard after provisioning.</p></details>
  <footer><span>Local control. A direct connection.</span><span>Readings update automatically · No cloud account</span></footer>
</main>
</div>
<script>
const token = new URLSearchParams(location.search).get('t') || '';
if (token) history.replaceState(null, '', location.pathname);
const $ = id => document.getElementById(id);
const error = $('error');
const views = new Map();
let busy = false, healthy = false, snapshot = null, reading = false;
let refreshTask = null;
let commandMessage = '', commandKind = 'success';
let historyGeneration = 0, lastHistoryFetch = 0, historyExportParams = null, exportingHistory = false;
const historyDevices = new Map();
const historyDeviceDetails = new Map();
function text(el, value) { if (el.textContent !== String(value)) el.textContent = String(value); }
function notice(message, kind) { text(error, message); error.dataset.kind = kind; }
function node(tag, value, parent, className = '') {
  const el = document.createElement(tag);
  el.className = className;
  if (value) text(el, value);
  parent.append(el);
  return el;
}
function format(value, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: digits}) : '—';
}
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme').setAttribute('aria-pressed', String(dark));
  text($('theme'), dark ? 'Light theme' : 'Dark theme');
}
try { applyTheme(localStorage.getItem('powerk-theme') === 'dark'); } catch (e) { applyTheme(false); }
$('theme').onclick = () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  applyTheme(dark);
  try { localStorage.setItem('powerk-theme', dark ? 'dark' : 'light'); } catch (e) {}
};
$('setup-button').onclick = () => { $('setup').open = true; $('setup').scrollIntoView({block:'nearest'}); };
$('refresh').onclick = () => refresh();
for (const id of ['history-range', 'history-device', 'history-outlet']) $(id).onchange = () => { historyDeviceOptions([]); loadHistory(); };
$('history-device').onblur = () => historyDeviceOptions([]);
$('history-refresh').onclick = () => loadHistory();
$('history-export').onclick = () => exportHistory();

function historyNotice(message, kind = '') {
  text($('history-message'), message);
  $('history-message').dataset.kind = kind;
}
function historyDeviceOptions(devices) {
  for (const device of devices) {
    historyDeviceDetails.set(device.mac, {...historyDeviceDetails.get(device.mac), ...device});
  }
  // Never change choices under the pointer while the browser's picker is open.
  try {
    if ($('history-device').matches?.(':open')) return;
  } catch (error) { /* Older browsers may not recognize :open. Keep native selects working. */ }
  for (const device of historyDeviceDetails.values()) {
    if (!historyDevices.has(device.mac)) {
      const option = node('option', '', $('history-device'));
      option.value = device.mac;
      const copy = node('span', '', option, 'option-copy');
      const label = node('span', '', copy, 'option-label');
      node('span', ' — ', copy, 'option-separator');
      const detail = node('span', '', copy, 'option-detail');
      historyDevices.set(device.mac, {option, label, detail});
    }
    const view = historyDevices.get(device.mac);
    text(view.label, device.name || 'Power strip');
    text(view.detail, `${device.model ? device.model + ' · ' : ''}${device.mac}`);
  }
}
function updateRecording(status) {
  if (!status?.enabled) {
    text($('history-recording'), 'Recorder unavailable');
    text($('history-saved'), 'Restart the server with consumption history enabled.');
    return;
  }
  text($('history-recording'), status.error ? 'Storage error' : 'Recording');
  const saved = status.last_recorded_at ? new Date(status.last_recorded_at * 1000).toLocaleString() : 'waiting for the first reading';
  text($('history-saved'), `Saved every ${status.interval_seconds} seconds · Last saved: ${saved} · ${status.failed_snapshots || 0} failed snapshots`);
  if (status.error) historyNotice(status.error, 'error');
  historyDeviceOptions(snapshot.devices);
  if (Date.now() - lastHistoryFetch >= 60000) loadHistory();
}
async function loadHistory() {
  const generation = ++historyGeneration;
  lastHistoryFetch = Date.now();
  historyExportParams = null;
  $('history-export').disabled = true;
  historyNotice('Loading saved readings…');
  const days = [1, 7, 30].includes(Number($('history-range').value)) ? Number($('history-range').value) : 7;
  const end = Date.now() / 1000;
  const params = new URLSearchParams({start:String(end - days * 86400), end:String(end), bucket:days === 1 ? 'hour' : 'day'});
  if ($('history-device').value) params.set('mac', $('history-device').value);
  if ($('history-outlet').value) params.set('outlet', $('history-outlet').value);
  try {
    const report = await api(`api/history?${params}`);
    if (generation !== historyGeneration) return;
    historyDeviceOptions(report.devices);
    text($('history-energy'), format(report.summary.energy_kwh, 3));
    text($('history-readings'), format(report.summary.readings, 0));
    text($('history-flags'), format(report.summary.resets + report.summary.gaps, 0));
    $('history-rows').replaceChildren();
    for (const bucket of report.buckets) {
      const row = node('tr', '', $('history-rows'));
      const dateOptions = {timeZone:'UTC', month:'short', day:'numeric', year:'numeric'};
      if (report.bucket === 'hour') Object.assign(dateOptions, {hour:'2-digit', minute:'2-digit'});
      node('td', new Date(bucket.timestamp * 1000).toLocaleString(undefined, dateOptions), row);
      node('td', format(bucket.energy_kwh, 3), row);
      node('td', format(bucket.readings, 0), row);
      node('td', format(bucket.resets + bucket.gaps, 0), row);
    }
    const unallocated = report.summary.unallocated_energy_kwh;
    $('history-boundaries').hidden = !unallocated;
    text($('history-boundaries'), `${format(unallocated, 3)} kWh spans bucket boundaries and is included in the period total only.`);
    historyNotice(report.summary.readings ? 'Saved history · UTC buckets · CSV exports the selected period and outlets.' : 'No saved readings in this period. History starts from the first recorded sample.');
    if (snapshot?.history?.error) historyNotice(snapshot.history.error, 'error');
    historyExportParams = params.toString();
    $('history-export').disabled = exportingHistory;
  } catch (error) {
    if (generation === historyGeneration) {
      text($('history-energy'), '—');
      text($('history-readings'), '—');
      text($('history-flags'), '—');
      $('history-rows').replaceChildren();
      $('history-boundaries').hidden = true;
      historyNotice(`History unavailable: ${error.message}`, 'error');
    }
  }
}
async function exportHistory() {
  if (!historyExportParams || exportingHistory) return;
  exportingHistory = true;
  $('history-export').disabled = true;
  const params = historyExportParams;
  try {
    const response = await fetch(`api/history.csv?${params}`, {headers:{'X-Token':token}, signal:AbortSignal.timeout(60000)});
    if (!response.ok) throw Error(`HTTP ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = node('a', '', document.body);
    link.href = url;
    link.download = 'powerk-history.csv';
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    historyNotice('CSV downloaded for the selected period and outlets.');
  } catch (error) {
    historyNotice(`Export failed: ${error.message}`, 'error');
  } finally {
    exportingHistory = false;
    $('history-export').disabled = !historyExportParams;
  }
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: {'Content-Type':'application/json', 'X-Token':token},
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(12000)
  });
  const value = await response.text();
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = JSON.parse(value).error || message; } catch (e) {}
    throw Error(message);
  }
  return JSON.parse(value);
}
async function cmd(mac, outlet, on) {
  const device = snapshot?.devices.find(d => d.mac === mac);
  if (busy || !healthy || !device?.online) return;
  busy = true;
  commandKind = 'pending';
  commandMessage = outlet ? `Updating outlet ${outlet}…` : `Turning all outlets ${on ? 'on' : 'off'}…`;
  notice(commandMessage, commandKind);
  render(snapshot);
  try {
    const result = await api('api/onoff', {mac, outlet, on});
    if (!result.confirmed) throw Error('Outlet state was not confirmed');
    commandMessage = 'Outlet state confirmed.';
    commandKind = 'success';
    notice(commandMessage, commandKind);
    if (refreshTask) await refreshTask;
    await refresh();
  } catch (e) {
    commandMessage = e.message;
    commandKind = 'error';
    notice(commandMessage, commandKind);
  } finally {
    busy = false;
    render(snapshot);
  }
}
function createStrip() {
  const card = node('article', '', $('app'), 'strip');
  const header = node('div', '', card, 'strip-header');
  const heading = node('div', '', header, 'strip-heading');
  node('span', '⏻', heading, 'strip-icon').setAttribute('aria-hidden', 'true');
  const titleBlock = node('div', '', heading);
  const title = node('h3', '', titleBlock);
  const caption = node('div', '', titleBlock, 'strip-caption');
  const actions = node('div', '', header, 'actions');
  const badge = node('span', '', actions, 'pill');
  const allOn = node('button', 'All on', actions);
  const allOff = node('button', 'All off', actions, 'all-off');
  const summary = node('div', '', card, 'strip-summary');
  const grid = node('div', '', card, 'grid');
  const usbInfo = node('section', '', card, 'usb-info');
  usbInfo.hidden = true;
  node('h4', 'USB 1 + USB 2 · Always-powered design', usbInfo);
  node('p', 'No USB software switch or separate power/energy readings are exposed by this stock firmware. All on / All off controls above apply to AC outlets only; USB charging is not included in the consumption history.', usbInfo);
  const details = node('details', '', card, 'device-details');
  node('summary', 'Device details', details);
  const metadata = node('p', '', details, 'device-detail-text');
  return {card, title, caption, badge, allOn, allOff, summary, grid, usbInfo, metadata, outlets:new Map()};
}
function createOutlet(grid) {
  const card = node('div', '', grid, 'outlet');
  const top = node('div', '', card, 'outlet-top');
  node('span', '', top, 'socket').setAttribute('aria-hidden', 'true');
  const toggle = node('button', '', top, 'toggle');
  toggle.setAttribute('role', 'switch');
  node('span', '', toggle, 'toggle-track').setAttribute('aria-hidden', 'true');
  const title = node('h4', '', card, 'outlet-label');
  const state = node('p', '', card, 'outlet-state');
  const power = node('div', '', card, 'outlet-power');
  const watts = node('span', '', power);
  node('small', 'W', power);
  const detail = node('div', '', card, 'outlet-detail');
  return {card, toggle, title, state, watts, detail};
}
function render(s) {
  if (!s) return;
  $('refresh').disabled = busy || reading;
  const devices = s.devices;
  const online = healthy ? devices.filter(d => d.online) : [];
  const outlets = online.flatMap(d => d.outlets);
  const connected = online.length;
  const total = devices.reduce((sum, d) => sum + (d.energy_kwh || 0), 0);
  text($('connection'), healthy ? 'Server connected' : 'Connection lost');
  $('connection').className = healthy ? 'status live' : 'status';
  text($('total-power'), connected ? format(online.reduce((sum,d) => sum + d.power_w, 0)) : '—');
  text($('power-note'), connected ? `${connected} strip${connected === 1 ? '' : 's'} reporting live` : 'No live readings available');
  text($('active-outlets'), connected ? outlets.filter(o => o.on).length : '—');
  text($('outlet-total'), connected ? `of ${outlets.length}` : '');
  text($('total-energy'), devices.length ? format(total, 3) : '—');
  text($('energy-note'), healthy && connected === devices.length ? 'Device-reported cumulative total' : 'Last reported total · may be stale');
  text($('device-count'), `${connected} online · ${devices.length} known`);
  text($('hint'), `Server ${s.local_ip} · Device connection: TCP ${s.port}`);
  text($('provision'), `python powerk.py provision --ip ${s.local_ip} --ssid YOUR_WIFI --password YOUR_PASSWORD`);
  $('empty').hidden = devices.length > 0;
  text($('empty-title'), healthy ? 'Ready for your first connection' : 'Unable to reach your server');
  text($('empty-body'), healthy ? 'Connect a power strip to see live readings and control each outlet from here.' : 'Check that Powerk is running and this browser can reach it. We will keep trying.');
  for (const [mac, view] of views) {
    if (!devices.some(d => d.mac === mac)) { view.card.remove(); views.delete(mac); }
  }
  for (const d of devices) {
    if (!views.has(d.mac)) views.set(d.mac, createStrip());
    const v = views.get(d.mac);
    const live = healthy && d.online;
    text(v.title, d.name);
    text(v.caption, `${d.model} · Firmware ${d.fw} · 4 AC outlets${d.usb?.port_count === 2 ? ' + 2 USB charging ports' : ''}`);
    v.usbInfo.hidden = d.usb?.port_count !== 2;
    text(v.badge, live ? 'Online' : 'Offline / stale');
    v.badge.className = live ? 'pill' : 'pill offline';
    v.allOn.disabled = v.allOff.disabled = busy || !live;
    v.allOn.onclick = () => cmd(d.mac, 0, true);
    v.allOff.onclick = () => cmd(d.mac, 0, false);
    v.allOn.setAttribute('aria-label', `Turn all outlets on for ${d.name}`);
    v.allOff.setAttribute('aria-label', `Turn all outlets off for ${d.name}`);
    const seen = d.last_seen ? new Date(d.last_seen * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : 'never';
    text(v.summary, `${format(d.power_w)} W ${live ? 'live' : 'last reported'}  ·  ${format(d.energy_kwh, 3)} kWh  ·  ${format(d.voltage)} V  ·  ${format(d.current_a, 2)} A  ·  Wi-Fi ${format(d.rssi, 0)} dBm  ·  Updated ${seen}`);
    text(v.metadata, `Model: ${d.model} · Firmware: ${d.fw} · MAC: ${d.mac} · Current: ${format(d.current_a, 2)} A · Wi-Fi: ${format(d.rssi, 0)} dBm`);
    for (const [n, outlet] of v.outlets) {
      if (!d.outlets.some(o => o.n === n)) { outlet.card.remove(); v.outlets.delete(n); }
    }
    for (const o of d.outlets) {
      if (!v.outlets.has(o.n)) v.outlets.set(o.n, createOutlet(v.grid));
      const out = v.outlets.get(o.n);
      out.card.className = o.on ? 'outlet is-on' : 'outlet';
      text(out.title, `Outlet ${o.n}`);
      text(out.state, !live ? 'Last known state · stale' : busy ? 'Command in progress…' : o.on ? 'Powered on' : 'Powered off');
      text(out.watts, format(o.power_w));
      text(out.detail, `${format(o.temp_c, 0)} °C · ${format(o.energy_kwh, 3)} kWh`);
      out.toggle.disabled = busy || !live;
      out.toggle.setAttribute('aria-checked', String(o.on));
      out.toggle.setAttribute('aria-label', `Outlet ${o.n} on ${d.name}`);
      out.toggle.onclick = () => cmd(d.mac, o.n, !o.on);
    }
  }
}
function refresh() {
  if (refreshTask) return refreshTask;
  refreshTask = fetchState().finally(() => { refreshTask = null; });
  return refreshTask;
}
async function fetchState() {
  reading = true;
  $('refresh').disabled = true;
  try {
    snapshot = await api('api/state');
    healthy = true;
    text($('updated'), `Last synced ${new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'})}`);
    notice(commandMessage, commandKind);
    render(snapshot);
    updateRecording(snapshot.history);
  } catch (e) {
    healthy = false;
    notice(`Connection lost: ${e.message}`, 'error');
    text($('connection'), 'Connection lost');
    $('connection').className = 'status';
    text($('history-recording'), 'Recorder status unavailable');
    text($('updated'), 'Live updates paused · retrying automatically');
    if (snapshot) render(snapshot);
    else {
      text($('device-count'), 'Server unavailable');
      text($('empty-title'), 'Unable to reach your server');
      text($('empty-body'), 'Check that Powerk is running and this browser can reach it. We will keep trying.');
    }
  } finally {
    reading = false;
    $('refresh').disabled = busy;
  }
}
async function poll() {
  if (!busy) await refresh();
  setTimeout(poll, 2000);
}
poll();
</script>
</body>
</html>
"""


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.workers = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class Web(BaseHTTPRequestHandler):
    hub: Hub
    loop: asyncio.AbstractEventLoop
    local_ip: str
    port: int
    token: str = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, fmt, *args):
        pass

    def _authorized(self) -> bool:
        """No token set -> open. Otherwise: X-Token header, HTTP Basic (browser) or ?t= (simple clients)."""
        if not self.token:
            return True
        if secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
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
        body = b"powerk: token required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="powerk"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _history_response(self, path):
        history = self.hub.history
        if history is None:
            self._json({"error": "history recording is not enabled"}, 503)
            return
        try:
            params = History.parameters(urlparse(self.path).query)
        except ValueError as error:
            self._json({"error": str(error)}, 400)
            return
        if path == "/api/history":
            try:
                report = history.report(params)
            except sqlite3.Error:
                self._json({"error": "history storage unavailable"}, 503)
                return
            self._json(report)
            return
        with closing(history.csv_chunks(params)) as chunks:
            try:
                first = next(chunks)
            except sqlite3.Error:
                self._json({"error": "history storage unavailable"}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="powerk-history.csv"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(first)
                for chunk in chunks:
                    self.wfile.write(chunk)
            except (ConnectionError, OSError, sqlite3.Error):
                self.close_connection = True

    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/powerk.py", "/powerk.apk") and not self._authorized():
            self._deny()
            return
        if path == "/api/state":
            future = asyncio.run_coroutine_threadsafe(self.hub.state(), self.loop)
            try:
                snap = future.result(timeout=2)
            except Exception:
                future.cancel()
                self._json({"error": "state unavailable"}, 503)
                return
            snap["local_ip"], snap["port"] = self.local_ip, self.port
            self._json(snap)
        elif path in ("/api/history", "/api/history.csv"):
            self._history_response(path)
        elif path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/powerk.py":               # public: lets a phone/borrowed laptop fetch this file to run `provision`
            body = Path(__file__).resolve().read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python")
            self.send_header("Content-Disposition", 'attachment; filename="powerk.py"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/powerk.apk":              # public: the Android app, once built (android/ -> gradlew assembleDebug)
            apk = Path(__file__).resolve().parent / "android/app/build/outputs/apk/debug/app-debug.apk"
            if not apk.is_file():
                self._json({"error": "no APK built yet: cd android && gradlew assembleDebug"}, 404)
                return
            body = apk.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Disposition", 'attachment; filename="powerk.apk"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/onoff":
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._deny()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                raise ValueError("invalid body length")
            self.connection.settimeout(3)
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict) or not isinstance(req.get("mac"), str):
                raise ValueError("invalid device")
            mac = req["mac"].upper()
            outlet, on = req.get("outlet"), req.get("on")
            if (not re.fullmatch(r"[0-9A-F]{12}", mac) or type(outlet) is not int
                    or outlet not in range(5) or type(on) is not bool):
                raise ValueError("invalid command")
        except (ValueError, TypeError, OSError):
            self._json({"error": "bad request"}, 400)
            return

        channels = [1, 2, 3, 4] if outlet == 0 else [outlet]
        fut = asyncio.run_coroutine_threadsafe(self.hub.command(mac, channels, on), self.loop)
        try:
            ok = fut.result(timeout=COMMAND_TIMEOUT + 1)
        except ConnectionError:
            self._json({"error": "device offline"}, 503)
            return
        except (asyncio.TimeoutError, FutureTimeoutError):
            fut.cancel()
            self._json({"error": "command timed out; refresh state before retrying"}, 504)
            return
        except Exception:
            fut.cancel()
            self._json({"error": "command failed"}, 502)
            return
        self._json({"ok": bool(ok), "confirmed": ok,
                    **({} if ok else {"error": "outlet state was not confirmed"})}, 200 if ok else 502)


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
        raise SystemExit(f"strip answered {text!r}, expected {expect!r}")
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


async def cmd_serve(args) -> int:
    history = History(args.history_db, args.history_interval)
    history.start()
    loop = asyncio.get_running_loop()
    termination_handler = False
    try:
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        termination_handler = True
    except (NotImplementedError, RuntimeError):
        pass
    try:
        return await _run_server(args, history)
    except asyncio.CancelledError:
        return 0
    finally:
        await loop.run_in_executor(None, history.close)
        if termination_handler:
            loop.remove_signal_handler(signal.SIGTERM)


async def _run_server(args, history: History) -> int:
    hub = Hub(set(args.allow_device), history=history)
    server = await asyncio.start_server(hub.handle_client, args.device_bind, args.port, limit=8192)
    candidates = local_ipv4s()
    ip = args.ip or (candidates or ["127.0.0.1"])[0]

    Web.hub, Web.loop, Web.local_ip, Web.port = hub, asyncio.get_running_loop(), ip, args.port
    Web.token = args.token
    try:
        httpd = BoundedHTTPServer((args.web_bind, args.web_port), Web)
    except Exception:
        server.close()
        await server.wait_closed()
        raise
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(f"powerk")
    print(f"  web UI (phone/PC)  http://{ip}:{args.web_port}")
    print(f"  strip server       TCP {args.port}")
    print(f"  consumption log    {history.path} (every {history.interval:g} seconds)")
    print(f"  auth               {'token required' if args.token else 'NONE - only safe on a trusted LAN'}")
    if args.token:
        print(f"                     browser: user 'powerk' + the token as password; app: Settings -> Token")
    print(f"  if not provisioned yet:  python powerk.py provision --ip {ip} --ssid WIFI --password PW")
    if (Path(__file__).resolve().parent / "android/app/build/outputs/apk/debug/app-debug.apk").is_file():
        print(f"  android app        http://{ip}:{args.web_port}/powerk.apk")
    if len(candidates) > 1 and not args.ip:
        print(f"  other local IPs: {', '.join(c for c in candidates if c != ip)}  (pick one with --ip if wrong)")
    print("  Ctrl+C to stop")
    try:
        async with server:
            await hub.poll_loop()
    finally:
        await asyncio.get_running_loop().run_in_executor(None, httpd.shutdown)
        httpd.server_close()
        for session in list(hub.sessions.values()):
            session.writer.close()
    return 0


def _fake_tags(state_on: set[int]) -> str:
    """A 'up:getinfo:' payload shaped like the real strip's, for offline checks."""
    return ":".join(
        f"{n}:{n * 111};{'on' if n in state_on else 'off'};{int(n in state_on)};0;0;"
        f"{5300 if n == 2 and n in state_on else 0};0000000A;00000000;00000000;0;00;{30 + n}"
        for n in (1, 2, 3, 4)
    )


async def _selftest() -> None:
    hub = Hub()
    server = await asyncio.start_server(hub.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    # A stand-in for the strip: answers getinfo, records onoff commands, keeps relay state.
    state_on = {2}

    async def fake_strip():
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode().strip()
            if line == "up:getinfo:all":
                writer.write(f"up:getinfo:{_fake_tags(state_on)}\r\n".encode())
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
    assert dev.outlets[1]["on"] is False and dev.outlets[2]["on"] is True
    assert dev.outlets[2]["power_w"] == 5.3 and dev.outlets[2]["energy_kwh"] == 0.01
    assert dev.outlets[1]["temp_c"] == 31 and dev.outlets[4]["temp_c"] == 34
    assert dev.voltage_v == 224.5 and dev.rssi == -52
    assert dev.current_a == 0.02

    assert await hub.session("aabbccddeeff").set_outlets([2], False) is True
    assert dev.outlets[2]["on"] is False and state_on == set()
    snapshot = hub.snapshot()["devices"][0]
    assert snapshot["on"] is False and snapshot["energy_kwh"] == 0.04

    strip_task.cancel()
    writer.close()
    server.close()
    print("selftest: parser + command round-trip OK")


def device_mac(value: str) -> str:
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", value):
        raise argparse.ArgumentTypeError("device MAC must contain exactly 12 hexadecimal characters")
    return value.upper()


def history_interval(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 5 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("history interval must be between 5 and 3600 seconds")
    return seconds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run device server + web UI (default)")
    s.add_argument("--port", type=int, default=DEVICE_PORT, help=f"device TCP port (default {DEVICE_PORT}, the strip's fixed port)")
    s.add_argument("--web-port", type=int, default=WEB_PORT)
    s.add_argument("--ip", help="IP to print in the UI/provision hint (default: auto-detect)")
    s.add_argument("--token", default="", help="require this token (password) for the web UI/API; use it whenever the port is reachable from the internet")
    s.add_argument("--allow-device", action="append", default=[], type=device_mac, help="allowed 12-hex MAC; repeat for multiple strips (enrollment filter, not authentication)")
    s.add_argument("--web-bind", default="0.0.0.0", help="web listener address; use 127.0.0.1 behind an HTTPS proxy")
    s.add_argument("--device-bind", default="0.0.0.0", help="device listener address")
    s.add_argument("--history-db", default=str(HISTORY_DB), help="SQLite file for persistent consumption history")
    s.add_argument("--history-interval", type=history_interval, default=HISTORY_INTERVAL, help="seconds between saved snapshots per strip (default: 30; range: 5..3600)")
    p = sub.add_parser("provision", help="send home Wi-Fi + this PC's IP to a strip in setup mode")
    p.add_argument("--ip", help="IPv4 this PC will have on the home Wi-Fi (what the strip connects to)")
    p.add_argument("--ssid", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--host", default=SETUP_HOST, help=f"strip setup address (default {SETUP_HOST})")
    p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the strip setup service")
    sub.add_parser("selftest", help="offline protocol check")
    argv = sys.argv[1:] or ["serve"]
    args = ap.parse_args(argv)

    if args.cmd == "provision":
        return cmd_provision(args)
    if args.cmd == "selftest":
        asyncio.run(_selftest())
        return 0
    if args.cmd == "serve":
        try:
            return asyncio.run(cmd_serve(args))
        except KeyboardInterrupt:
            return 0
    ap.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
