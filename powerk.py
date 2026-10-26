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
import ipaddress
import json
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import TimeoutError as FutureTimeoutError
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
        })
    return out


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
    def __init__(self, allowed_devices: set[str] | None = None) -> None:
        self.devices: dict[str, Device] = {}
        self.sessions: dict[str, Session] = {}
        self.allowed_devices = allowed_devices or set()
        self.connections = 0

    def session(self, mac: str) -> Session | None:
        s = self.sessions.get(mac.upper())
        if s and s.device and s.device.online and not s.writer.is_closing():
            return s
        return None

    def snapshot(self) -> dict:
        return {"devices": [d.snapshot() for d in self.devices.values()]}

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
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>powerk</title>
<style>
:root{--bg:#0f1115;--card:#191d24;--on:#2ecc71;--off:#39424f;--fg:#e8ecf1;--dim:#8b96a5}
*{box-sizing:border-box}
body{margin:auto;padding:16px;max-width:720px;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}
h1{font-size:20px;margin:0}
.hint{color:var(--dim);font-size:13px;margin:6px 0 16px}
.card{background:var(--card);border-radius:14px;padding:14px;margin-bottom:16px}
.card.off{opacity:.45}
.head{display:flex;justify-content:space-between;align-items:center;gap:10px}
.head button{width:auto;padding:10px 16px;flex-shrink:0}
#error{color:#ffb4ab;margin:8px 0 16px}
#error:empty{display:none}
.mac{font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;background:#c0392b}
.dot.on{background:var(--on)}
.stats{color:var(--dim);font-size:13px;margin:8px 0 12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
button{border:0;border-radius:12px;padding:14px 10px;font:inherit;color:var(--fg);background:var(--off);text-align:left;width:100%;cursor:pointer}
button.on{background:var(--on);color:#06130a}
button:disabled{cursor:default;opacity:.6}
button small{display:block;opacity:.85;font-size:12px}
</style>
<h1>powerk</h1>
<div class=hint id=hint></div>
<div role=alert id=error></div>
<div id=app></div>
<script>
const token=new URLSearchParams(location.search).get('t')||'';
if(token) history.replaceState(null,'',location.pathname);
let busy=false, healthy=false, snapshot=null;
const error=document.getElementById('error');
async function api(p,b){
  const response=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json','X-Token':token},
    body:b?JSON.stringify(b):undefined,signal:AbortSignal.timeout(12000)});
  const text=await response.text();
  if(!response.ok){let message=`HTTP ${response.status}`;try{message=JSON.parse(text).error||message}catch(e){}throw Error(message)}
  return JSON.parse(text);
}
async function cmd(mac,outlet,on){
  if(busy||!healthy)return;
  busy=true;render(snapshot);error.textContent='Sending command…';
  try{const result=await api('api/onoff',{mac,outlet,on});if(!result.confirmed)throw Error('Outlet state was not confirmed');error.textContent='';await refresh()}
  catch(e){error.textContent=e.message}
  finally{busy=false;render(snapshot)}
}
function node(tag,text,parent){const el=document.createElement(tag);el.textContent=text;parent.append(el);return el}
function render(s){
  if(!s)return;
  document.getElementById('hint').textContent =
    `server ${s.local_ip} | strip connects to TCP ${s.port} | provision with: powerk.py provision --ip ${s.local_ip}`;
  const app=document.getElementById('app');app.replaceChildren();
  for(const d of s.devices){
    const online=d.online&&healthy;
    const card=node('div','',app);card.className=online?'card':'card off';
    const head=node('div','',card);head.className='head';
    node('span',`${d.name} · ${online?'online':'offline / stale'}`,head).className='mac';
    const all=node('button',d.on?'ALL OFF':'ALL ON',head);
    all.className=d.on?'on':'';
    all.disabled=busy||!online;all.onclick=()=>cmd(d.mac,0,!d.on);
    const seen=d.last_seen?new Date(d.last_seen*1000).toLocaleTimeString():'never';
    node('div',`${d.model} fw ${d.fw} | ${d.power_w} W | ${d.energy_kwh} kWh | ${d.voltage??'—'} V | ${d.current_a??'—'} A | wifi ${d.rssi??'—'} dBm | last update ${seen}`,card).className='stats';
    const grid=node('div','',card);grid.className='grid';
    for(const o of d.outlets){
      const button=node('button',`Outlet ${o.n}`,grid);button.className=o.on?'on':'';
      button.disabled=busy||!online;button.onclick=()=>cmd(d.mac,o.n,!o.on);
      node('small',`${o.on?'ON':'OFF'} | ${o.power_w} W | ${o.temp_c} C`,button);
    }
  }
  if(!s.devices.length)node('div','No strip connected yet. Provision the strip, then power-cycle it.',app).className='card';
}
async function refresh(){try{snapshot=await api('api/state');healthy=true;if(!busy)error.textContent='';render(snapshot)}catch(e){healthy=false;error.textContent=`Connection lost: ${e.message}`;render(snapshot)}}
async function poll(){if(!busy)await refresh();setTimeout(poll,2000)}poll();
</script>
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
        self.end_headers()
        self.wfile.write(body)

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
    hub = Hub(set(args.allow_device))
    server = await asyncio.start_server(hub.handle_client, args.device_bind, args.port, limit=8192)
    candidates = local_ipv4s()
    ip = args.ip or (candidates or ["127.0.0.1"])[0]

    Web.hub, Web.loop, Web.local_ip, Web.port = hub, asyncio.get_running_loop(), ip, args.port
    Web.token = args.token
    httpd = BoundedHTTPServer((args.web_bind, args.web_port), Web)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(f"powerk")
    print(f"  web UI (phone/PC)  http://{ip}:{args.web_port}")
    print(f"  strip server       TCP {args.port}")
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
