# powerk

Local control for the LG U+ / Jinheung **MTTL-W01** 4-outlet smart strip — from a
phone, from a PC, from anywhere if you run the server on a VPS.

**English** | [العربية](README.ar.md)

| | |
|---|---|
| Server | `powerk.py` — one file, Python standard library only |
| App | `android/` — Kotlin + Jetpack Compose, Material 3 Expressive (apk ~12 MB) |
| Protocol | the strip's own `up:...` text protocol over TCP **10086** |
| Needs | no cloud account, no paid app, **no router DNS or DNAT change**, no port forwarding at home, no MQTT, no Home Assistant, no firmware flashing, no Docker |

---

## Contents

- [How it works](#how-it-works)
- [What's in here](#whats-in-here)
- [Requirements](#requirements)
- [Step 0 — provision the strip (once, for either mode)](#step-0--provision-the-strip-once-for-either-mode)
- [Mode A — Local (home network only)](#mode-a--local-home-network-only)
- [Mode B — Server (VPS, control from anywhere)](#mode-b--server-vps-control-from-anywhere)
- [Android app (`android/`)](#android-app-android)
- [Web UI and HTTP API](#web-ui-and-http-api)
- [Consumption history](#consumption-history)
- [Protocol reference](#protocol-reference)
- [Troubleshooting](#troubleshooting)
- [What this does not do (on purpose)](#what-this-does-not-do-on-purpose)
- [Need something built?](#need-something-built)

## How it works

The strip dials a server IP that it stores in its own flash memory. Provisioning
(one time) writes **your server's IP** into the strip; from then on the strip
opens a TCP session to `<server>:10086` and speaks its native line protocol.
`powerk.py` answers that session, keeps the live state, and serves a web UI + JSON
API. The Android app is just a client of that API.

```
phone / PC browser ──► http://SERVER:8080 ──► powerk.py ◄── TCP 10086 ── strip
                                                 │
                                            android app (http://SERVER:8080/api/...)
```

Because the strip connects **out**, nothing has to be opened on your home router
and the same server works on your LAN or on a public VPS.

Two ways to run it:

* **Mode A — Local**: `powerk.py` runs on a PC / Raspberry Pi in your home network.
  Only your home network can reach the UI.
* **Mode B — Server**: `powerk.py` runs on a VPS with a public IP. The strip dials
  the VPS from home; you control it from anywhere with the token.

Both modes use the same file and the same app.

## What's in here

| Path | What it is |
|---|---|
| `powerk.py` | the server: TCP 10086 device server, web UI + JSON API, the `provision` command, `selftest` |
| `android/` | Android app source (`gradlew assembleDebug`) |
| `README.md` / `README.ar.md` | this document, English / Arabic |

## Requirements

* LG U+ / Jinheung **MTTL-W01** strip (stock firmware is fine — tested on `0.1.54-1.0.66`)
* 2.4 GHz Wi-Fi network for the strip (the strip does not support 5 GHz)
* A machine that stays on: PC, Raspberry Pi, or a VPS
* Python 3.8+ on that machine (no `pip install`, nothing else)
* A phone (Android app) or any browser

---

## Step 0 — provision the strip (once, for either mode)

Provisioning tells the strip two things: which Wi-Fi to join, and which server IP
to dial. It is done over the strip's own setup access point.

1. **Start the server** on the machine that will stay on and note the IP it prints:

   ```bash
   python powerk.py
   ```

   ```
   powerk
     web UI (phone/PC)  http://192.168.1.14:8080
     strip server       TCP 10086
     auth               NONE - only safe on a trusted LAN
   ```

2. **Put the strip in setup mode**: hold its main button ~10 s, until the LED
   blinks fast.

3. **Join the strip's Wi-Fi** from the machine that runs `provision`:
   network `TONLY_TAP_XXXXXXX`, password `LGU_XXXXXXX` — the same 7 characters
   that follow `TONLY_TAP_`. No internet on that network is normal.

4. **Provision it** — use the IP from step 1 (Mode A) or your VPS public IP (Mode B):

   ```bash
   python powerk.py provision --ip 192.168.1.14 --ssid "YOURWIFI" --password "YOURWIFIPW"
   ```

   Expected output:

   ```
     up:ip:ip_ok
     up:connect:connect_ok
   ```

   The strip's protocol cannot carry `:` or newlines in the SSID/password; the
   command refuses instead of failing silently. `--ip` must be an IPv4 address.

5. **Reconnect that machine to your normal network.** Within seconds the strip
   connects to `<ip>:10086` and appears in the UI.

6. **Verify** on the server (or in its terminal):

   ```
   [device] MTTL 91C0C4C model=lgutap fw=0.1.54-1.0.66
   ```

To move the strip to another server later, repeat step 0 with the new IP — the
strip stores exactly **one** server address.

### If the machine has no Wi-Fi

The server machine never needs Wi-Fi to **run** powerk — Ethernet is fine, the
strip only has to reach its IP. Wi-Fi is needed once for the provisioning step by
any device that can join `TONLY_TAP_XXXXXXX` and open TCP to `192.168.1.1:30300`:

1. **Android phone, free** — on your home Wi-Fi install
   [Termux](https://f-droid.org/packages/com.termux/), run `pkg install python`,
   download the script with `curl -O http://<server-ip>:8080/powerk.py`, then join
   the strip's setup network and run the `provision` command above (with the
   *server's* IP).
2. **Any "TCP client" app** — join the strip's network, connect to `192.168.1.1`
   port `30300` and send these two lines, each in its own connection, with CRLF
   line endings:
   ```
   up:ip:<SERVER-IP>                ->  up:ip:ip_ok
   up:connect:<SSID>:<PASSWORD>     ->  up:connect:connect_ok
   ```
3. **USB Wi-Fi dongle (~$10)** on the server machine — then just follow step 0.
4. **A borrowed laptop for two minutes** — download `powerk.py` from
   `http://<server-ip>:8080/powerk.py` and run the same command with the
   **server's** IP, not the laptop's.

The bundled `MTTL-W01-Provisioner.apk` cannot do this step: it only stores Wi-Fi
credentials, never a server IP.

---

## Mode A — Local (home network only)

1. On the PC / Raspberry Pi:

   ```bash
   python powerk.py                 # prints http://<ip>:8080
   ```

2. Provision the strip with that IP (step 0 above).

3. Open `http://<ip>:8080` in any browser on the same network — 4 outlet toggles,
   ALL ON/OFF, live watts, kWh, temperature, voltage, Wi-Fi signal.

4. In the Android app: **Settings → Server IP** = that IP, port `8080`, token
   empty → **Save** → **Test** → `Connected · 1 strip(s), 1 online`.

Notes:

* Give the machine a **fixed IP or DHCP reservation** — the strip remembers the
  address you provisioned, so a changed IP means re-running step 0.
* The machine must be awake; the strip reconnects by itself after a sleep, reboot
  or restart of `powerk`.
* Windows firewall: allow the Python prompt on first run, or
  ```powershell
  netsh advfirewall firewall add rule name="powerk" dir=in action=allow protocol=TCP localport=8080,10086
  ```

---

## Mode B — Server (VPS, control from anywhere)

The strip dials the VPS from home; you reach the UI from any network.

### 1. Copy the server

```powershell
scp X:\powerk\powerk.py ubuntu@<VPS-IP>:~/powerk/
```

### 2. Run it as a service

```bash
cd ~/powerk
TOKEN=$(openssl rand -hex 16); echo "YOUR TOKEN: $TOKEN"     # save this
PUB=$(curl -s https://api.ipify.org)                          # your public IPv4
sudo tee /etc/systemd/system/powerk.service >/dev/null <<EOF
[Unit]
Description=powerk MTTL-W01 server
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/powerk
ExecStart=/usr/bin/python3 /home/ubuntu/powerk/powerk.py serve --ip $PUB --web-port 7896 --token $TOKEN
Restart=always
RestartSec=3
User=ubuntu

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now powerk
curl -s -o /dev/null -w 'local code: %{http_code}\n' http://127.0.0.1:7896/     # 401 = running + token active
ss -lntp | grep -E '7896|10086'
```

Adapt `User=` / `WorkingDirectory=` / the port to your setup. `--web-port 7896`
is only an example; `--port 10086` can never change (the strip's firmware dials
10086).

### 3. Open the ports

In your VPS provider's firewall/security group (and in `ufw` if you use it):

| Port | For | Who may reach it |
|---:|---|---|
| **10086/tcp** | the strip | the internet (the strip comes from a dynamic home IP) |
| **7896/tcp** | web UI + API | you — ideally only through a VPN/tunnel, see below |
| 22/tcp | ssh | you |

Verify from your own PC:

```powershell
Test-NetConnection <VPS-IP> -Port 10086
Test-NetConnection <VPS-IP> -Port 7896
```

### 4. Provision the strip with the public IP

At home, follow step 0 with `--ip <VPS-IP>`, then watch the VPS:

```bash
journalctl -u powerk -f      # [device] MTTL 91C0C4C model=lgutap fw=0.1.54-1.0.66
```

### 5. Use it

* **App**: Settings → host `<VPS-IP>`, port `7896`, token → Save → Test.
* **Browser**: `http://<VPS-IP>:7896/` → login user `powerk`, password = token
  (or open `http://<VPS-IP>:7896/?t=TOKEN` once).

### Keeping the UI private (optional but recommended)

Protect both listeners. The strip's TCP protocol has no authentication or
encryption: a client can claim a MAC address and send fabricated telemetry.
Duplicate live sessions are rejected, but this does not authenticate a device.
Keep TCP 10086 on a trusted LAN or route the home network through a VPN to the
VPS; the strip itself cannot run a VPN client. If public exposure is unavoidable,
restrict source addresses in the firewall where possible and enroll known MACs:

```bash
python powerk.py serve --token YOUR_TOKEN --allow-device AABBCCDDEEFF
```

Repeat `--allow-device` for multiple strips. Without this flag, any MAC can enroll
(convenient for initial LAN setup). An allowlist is a filter, not authentication;
an attacker who knows an allowed MAC can still impersonate a disconnected strip.
The server bounds incoming connections and closes unresponsive device sessions.
Protect the web UI/API as well:

* **Token** (`--token`) — required for every page/API call. Without it the UI is
  open to anyone who finds the port.
* **VPN / tunnel** instead of a public port: install
  [Tailscale](https://tailscale.com) on the VPS and on your phone, then use the
  `100.x.y.z` address in the app and keep the web port closed (`ufw allow in on
  tailscale0 to any port 7896 proto tcp`). Nothing is exposed and the traffic is
  encrypted.
* Cleartext HTTP means the token is visible to anyone on the network path. If that
  matters, put your existing nginx/Apache in front as an HTTPS reverse proxy (or
  use the VPN option above). Bind the backend with `--web-bind 127.0.0.1` and
  enable **Use HTTPS** in Android settings with the proxy's host and port. A
  trusted certificate is required; certificate validation is never bypassed.

`--device-bind` selects the TCP listener address; `--ip` only changes the address
shown in the UI and provisioning hints, not either listener's bind address.

---

## Android app (`android/`)

```bash
cd android
gradlew assembleDebug            # or: open the folder in Android Studio
```

Install on the phone:

* easiest — with the server running, open `http://<server>:<web-port>/powerk.apk`
  on the phone (the server serves the built APK; that URL needs no token), or
* `adb install android/app/build/outputs/apk/debug/app-debug.apk`

What you get: an animated splash, a live power ring (watts + energy/voltage/current/RSSI
pills), 4 animated outlet tiles with an in-tile switch, ALL ON/OFF, offline/error
states, Snackbars for failed commands, and a settings screen with server IP, port,
token, a connection Test, and a language switch.

Behavior details:

* Polls `/api/state` every 2 s while the app is visible; a press flips the tile immediately and shows a
  spinner in the switch while the strip confirms.
* Commands are blocked per outlet while one is in flight — no toggle storms.
* **Languages**: Arabic + English, following the phone by default; override in
  Settings (also appears in Android's per-app language menu).
* Debug build, no signing setup — fine for personal use, not for the Play Store.
* HTTP for trusted LANs; optional HTTPS for a reverse proxy with a trusted
  certificate. Tokens are masked in settings and app backup is disabled.

---

## Web UI and HTTP API

The responsive dashboard shows live power, active outlets, and the strip's
cumulative energy readings. Each strip has individual outlet switches and
separate **All on** / **All off** controls. Offline readings are marked as stale
and excluded from live totals. **Dark theme** is saved in this browser.

Model, firmware, watts, energy, voltage, current, and Wi-Fi signal are visible
in each strip's main view. Expand **Device details** for the full MAC address, or
**Connection & setup** for the server address and provisioning command. Live
updates preserve keyboard focus and expanded panels. After updating `powerk.py`,
restart the server process to load the new dashboard, then reload the browser.
The normal server page controls real connected strips through the same API as
the original interface; a separately labeled sample preview is not a device server.

| Method | Path | Notes |
|---|---|---|
| GET | `/` | the web UI |
| GET | `/api/state` | `{"local_ip":"…","port":10086,"devices":[{"mac","name","model","fw","online","on","power_w","energy_kwh","voltage","current_a","rssi","outlets":[…]}]}` |
| POST | `/api/onoff` | `{"mac":"…","outlet":0-4,"on":true}` — outlet `0` = all |
| GET | `/api/history` | Filtered consumption summary, UTC buckets, and historical device list |
| GET | `/api/history.csv` | Streaming raw readings for the selected period/device/outlet |
| GET | `/powerk.py` | the script itself (public) |
| GET | `/powerk.apk` | the built Android app, if present (public) |

When `--token` is set, every request needs it — any of:

* `X-Token: TOKEN` header (what the app sends)
* HTTP Basic, user `powerk` + token as password (what the browser does)
* `?t=TOKEN` query parameter

`/powerk.py` and `/powerk.apk` stay public on purpose, so a new phone can always
download the app and a machine without Wi-Fi can always download the provisioner.

Commands require a 12-hex MAC, an integer outlet `0..4`, and a JSON boolean `on`
(strings such as `"false"` are rejected). Bodies are limited to 4 KiB. A command
returns success only after all targeted relays report the requested state.
Unconfirmed commands return 502, offline devices 503, and expired commands 504.
The eight-second command deadline includes queueing; expiration cancels pending
work, but cannot undo relay commands already sent. Refresh before retrying.
State includes `last_seen`, the Unix timestamp of the latest complete status
report. A failed device poll marks the strip offline and closes its session.
The web UI disables offline/pending controls and reports connection failures.

## Consumption history

Recording runs on the server, even when the browser and Android app are closed.
Every **30 seconds per connected strip**, a complete snapshot saves all four
outlets to **`data/history.sqlite3`** beside `powerk.py`. SQLite is part of Python's
standard library; no database server or package installation is needed. Data
survives restarts and is **never automatically deleted**. Runtime database files
are excluded from Git.

The web dashboard's **Consumption history** section filters the last 24 hours,
7 days, or 30 days by strip and outlet. It shows measured counter increases,
saved reading counts, reset/gap flags, and an hourly/daily table using **UTC**.
Historical devices remain in the filter even when disconnected. **Export CSV**
downloads raw readings for those same filters, with the existing authentication.

Configure storage and sampling if needed:

```bash
python powerk.py serve --web-port 8081 --history-db /path/to/history.sqlite3 --history-interval 30
```

`--history-interval` accepts 5–3600 seconds. Use a disk location the server user
can write. Keep the server running to collect data. The recorder queues writes
in a background thread; history queries and CSV exports do not block the device
event loop. The UI reports the last saved timestamp and storage failures.

How consumption is calculated:

* The first saved reading for each outlet is a **baseline**, not new consumption.
  Powerk cannot recover detailed history from before recording started.
* Energy counters are stored as integer **Wh**. Consumption is the nonnegative
  difference between consecutive saved counters, not an estimate from watts.
* A lower counter is marked **`reset`** and contributes no delta. Its observed
  value is retained as the new baseline; detected resets force an early save.
  Usage around unobserved resets can be
  undercounted; the application does not invent it.
* An interval longer than `max(120 seconds, 3 × sampling interval)` is marked
  **`gap`**. A nonnegative counter increase across that interval is preserved,
  but its timing inside the gap is unknown. Flags count per-outlet intervals.
* A period total includes a delta only when both saved observations are within
  `[start, end)`. A UTC bucket includes it only when both observations also lie
  in that bucket. Deltas crossing bucket boundaries are shown separately as
  `unallocated_energy_kwh`, rather than assigned to the wrong hour/day.
* Offline/stale snapshots are not saved as new readings. Disk/queue failures
  are surfaced; the next saved counter may bridge a gap. No automatic retention
  policy is applied, so disk usage grows with time and connected strip count.

API filters for both endpoints: `start` and `end` are Unix seconds; optional
`mac` is a 12-hex device address; optional `outlet` is `1..4`; `bucket` is `hour`
or `day` (default `day`). The default period is the last seven days, with a
maximum range of 366 days per request. Ranges can be exported in consecutive
windows for longer-term analysis. Invalid filters return 400; unavailable storage
returns 503. There is no generated API schema in this repository.

`/api/state` also includes `history` recording status: `enabled`,
`interval_seconds`, `last_recorded_at`, `readings_saved`, `failed_snapshots`, and
`error`. Failure counts describe the current server process.

`/api/history` returns `start`, `end`, `bucket`, `timezone`, `devices`, `buckets`,
and `summary`. The summary includes `energy_kwh`, `readings`, `resets`, `gaps`,
`first_sample`, `last_sample`, and `unallocated_energy_kwh`.

CSV columns contain UTC timestamps and Unix seconds, MAC, outlet number, relay
state, watts, cumulative counter in Wh, temperature in °C, voltage, RSSI in dBm,
previous timestamp, interval length in seconds, delta Wh, and quality
(`baseline`, `ok`, `gap`, `reset`). Missing values/deltas are blank, not zero.
Preserve MACs as strings when importing into analytics tools.

Back up the database with SQLite's backup API (or its `.backup` command), not by
copying only the main file while the server is writing in WAL mode. CSV is useful
for analysis; a SQLite backup preserves the complete database and metadata.

## Development and validation

For UI changes, follow [UI_GUIDELINES.md](UI_GUIDELINES.md). It defines the neutral
palette, restrained blue accent, typography and spacing scales, accessible
controls, and required visual review. CI checks the tokens and contrast in both
themes alongside interaction behavior.

```bash
python -m unittest discover -s tests -v
python powerk.py selftest
node --test tests/*.test.cjs
cd android
./gradlew testDebugUnitTest lintDebug assembleDebug
```

CI runs the Python suite on 3.8 and 3.12, web UI tests on Node 20, and Android unit tests, lint, and a debug
build with JDK 17 and SDK 37. Reports and the debug APK are uploaded as artifacts.
Tests use localhost fake strips and HTTP servers; they require no physical strip.
There is no generated API specification; update both clients, tests, and the API
reference here when changing the contract.

## Protocol reference

Wire format the strip speaks on TCP 10086 :

| Direction | Frame |
|---|---|
| strip → server | `up:bootinfo:<model>;<mac>;<mac>;<fw>;connect` |
| strip → server | `up:getinfo:<ch>:<runtime>;<relay on/off>;<state>;<overload>;<overheat>;<power mW>;<energy hex Wh>;<prev hex>;<config hex>;<status>;<event hex>;<temp C>` ×4 |
| strip → server | `up:power_report:<ch>:<mV>` (≥50000) or `:<mA>` |
| strip → server | `up:query:<rssi>` |
| strip → server | `up:event:onoff:<1-4>:on|off` (physical button, `0` = master) |
| server → strip | `up:getinfo:all`, `up:onoff:<1-4>:on|off`, `up:power_report:1:vol`, `up:query:wifirssi` |
| setup AP `192.168.1.1:30300` | `up:ip:<ip>` → `up:ip:ip_ok`, `up:connect:<ssid>:<pw>` → `up:connect:connect_ok` |

There is no `up:onoff:0`; "all" is sent as a loop over outlets 1–4. The device pads
some frames with NUL bytes and the port is fixed at 10086.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Not reachable` during provision | the machine is not on `TONLY_TAP_*`, or the strip is not in setup mode (hold the button again) |
| App/browser shows no strip | strip must be on 2.4 GHz; the provisioned IP must still be the server's IP; watch the `[device]` lines in the server output / `journalctl -u powerk -f` |
| Worked, then stopped | server IP changed, machine slept, or the strip moved to another network → re-provision (step 0) |
| Strip flaps: connects then drops | some firmware chatter is normal, the strip re-dials; if it never stabilises, check Wi-Fi signal (`rssi`) and that only one `powerk` is running |
| `bad request` from the API | an older app build sent an empty body — update the APK |
| App crashes at launch | update to the latest APK (older builds had an outlet-index crash) |
| UI unreachable but 10086 works | the web port is not open, or you are on a network that cannot reach the VPS |
| Nothing responds at all | `python powerk.py selftest` proves the server logic without any hardware |

## What this does not do (on purpose)

MQTT/Home Assistant entities, Matter, firmware OTA, multi-user
accounts. Add them when you actually want them.

---

## Need something built?

I turn ideas into working apps, websites and systems — phones, browsers, servers,
and beyond. Careful engineering, honest scope, fair prices, results that actually
work. If this project was useful to you, yours can be next.

| | |
|---|---|
| **WhatsApp** | [wa.me/201093480689](https://wa.me/201093480689) — fastest reply |
| **Call** | [+20 109 348 0689](tel:+201093480689) |
| **Email** | [1ahmed.tohamy@gmail.com](mailto:1ahmed.tohamy@gmail.com) |

Tell me what you want to build; you get a clear plan and a price before anything
starts.


