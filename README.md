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
- [Automation, energy and alerts](#automation-energy-and-alerts)
- [Android app (`android/`)](#android-app-android)
- [Web UI and HTTP API](#web-ui-and-http-api)
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
| `powerk.py` | the server: TCP 10086 device server, rules engine, energy history, alerts, web UI + JSON API, the `provision` command, `selftest` |
| `API.md` | the HTTP API reference (v2) |
| `android/` | Android app source (`gradlew assembleDebug`) |
| `powerk.json` / `powerk.db` | created at runtime next to `powerk.py` (or in `--data DIR`): names, rules, settings, energy history, event log. Not committed, so back them up yourself if you care about them. `powerk.json` holds your push tokens. |
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

3. Open `http://<ip>:8080` in any browser on the same network. It has five tabs:
   - **Outlets:** toggles and names, ALL ON/OFF, live watts, temperature, voltage and Wi-Fi signal.
   - **Rules:** schedules, timers, cycles and auto-off.
   - **Energy:** a chart with cost and the monthly budget.
   - **Alerts:** the event log.
   - **Settings:** price, budget, limits and push.

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
ExecStart=/usr/bin/python3 /home/ubuntu/powerk/powerk.py serve --ip $PUB --web-port 7896 --token $TOKEN --mac 88D039XXXXXX
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

Adapt `User=` / `WorkingDirectory=` / the port to your setup. Replace `--mac` with your strip's MAC,
which the UI shows after its first connection; you can also leave `--mac` out for now and add it later. `--web-port 7896`
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

Port 10086 has to be reachable by the strip. Control commands only ever travel server → strip, so a
stranger who finds that port cannot switch anything. A stranger could still announce a fake strip, or
claim your strip's MAC and push its session off. Use `--mac` so only your strip is accepted. The MAC
travels in cleartext, so `--mac` is a filter, not authentication. A firewall rule that only allows your
home IP, where that is practical, is stronger. The **UI port** is the
one that can turn your outlets on and off, so protect it:

* **Token** (`--token`) — required for every page/API call. Without it the UI is
  open to anyone who finds the port.
* **VPN / tunnel** instead of a public port: install
  [Tailscale](https://tailscale.com) on the VPS and on your phone, then use the
  `100.x.y.z` address in the app and keep the web port closed (`ufw allow in on
  tailscale0 to any port 7896 proto tcp`). Nothing is exposed and the traffic is
  encrypted.
* Cleartext HTTP means the token is visible to anyone on the network path. If that
  matters, put your existing nginx/Apache in front as an HTTPS reverse proxy (or
  use the VPN option above).

---

## Automation, energy and alerts

All of this runs inside `powerk.py`, so it keeps working while your phone is off. Use the web UI tabs,
the Android app, or the [HTTP API](API.md).

**Names.** Rename the strip and each outlet (for example "Fridge" or "Heater"). The web UI and the app
show the names everywhere.

**Rules** (server-side, per strip, for any set of outlets):

| kind | what it does |
|---|---|
| Schedule | at `HH:MM` on chosen weekdays, switch ON or OFF |
| Timer | in N minutes, switch ON or OFF once, then the timer deletes itself |
| Cycle | ON for X minutes, OFF for Y minutes, repeating |
| Auto-off | when an outlet is ON and draws less than W watts for N minutes, switch it off (a charger is full, or a device sits in standby) |

- If the strip is offline when a schedule is due, the server logs `skipped` and does not replay the
  action later.
- A timer waits up to 5 minutes for the strip, then gives up.
- Times use the server's local clock, so set the right timezone on the VPS
  (`timedatectl set-timezone Africa/Cairo`).

**Energy.** The strip keeps a running energy counter per outlet. powerk stores the increase every hour
in `powerk.db` and shows it per day, week, month or year, with the cost at your price per kWh.

- The first reading after the server starts is only a baseline.
- Energy used while the server was down is spread over that gap when the strip reconnects.
- All figures are estimates from the strip's meter, not your electricity bill.

**Budget.** Set a monthly budget in your currency. powerk raises an alert at 50, 75, 90 and 100 % of it.

**Alerts.** The Alerts tab shows these events:

- the strip went offline (after 60 s) and came back
- an outlet's temperature reached the limit
- the strip's total power reached the limit
- budget steps
- rules that ran, were skipped or were not confirmed
- auto-off actions

To get them on your phone, set one or both channels in Settings, then press **Send test**:

- **ntfy:** install the [ntfy](https://ntfy.sh) app, subscribe to a long random topic name, and paste
  `https://ntfy.sh/<topic>`. Anyone who knows the topic can read it.
- **Telegram:** create a bot with @BotFather, send it any message, and enter the bot token and your
  chat id.

**Debugging.** `GET /api/debug` shows the last raw `up:getinfo` line from each strip.

## Android app (`android/`)

```bash
cd android
gradlew assembleDebug            # or: open the folder in Android Studio
```

Install on the phone:

* easiest — with the server running, open `http://<server>:<web-port>/powerk.apk`
  on the phone (the server serves the built APK; that URL needs no token), or
* `adb install android/app/build/outputs/apk/debug/app-debug.apk`

What you get:

* **Outlets**: a live summary (watts, V·A, today's kWh, this month's cost), four outlet tiles
  that fill with colour when on, ALL ON / ALL OFF, rename, offline and error states.
* **Rules, Energy, Alerts**: the same schedule / timer / cycle / auto-off rules, the
  day-to-year energy chart with cost and budget, and the alert history as the web UI.
* **Settings**: server address, port and token with a connection test, language, theme,
  and **Strip info** (Wi-Fi signal in words, network address, model, firmware, MAC).
* **Three themes**, the same as the web UI: *Kraft paper* (default), *White & blue*
  (follows the phone's light/dark mode, or pin it) and *Chalkboard*. The fonts are bundled
  (see `android/FONTS.md`), so nothing is downloaded.
* **Home-screen widgets** (they follow the chosen theme):
  * *Outlet* — one outlet; tap to switch it. 1x1 up to 2x2.
  * *Whole strip* — strip name, total watts and four cells, each switching its own outlet.
    4x1, 2x2 or a short row.
  * *Usage* — watts now, today's kWh and cost, this month, and % of the monthly budget.
    Read-only; tap opens the Energy tab.
* **Quick Settings tile** for one chosen outlet.

Behavior details:

* Polls `/api/state` every 2 s; a press flips the tile immediately and shows the pending
  state while the strip confirms.
* Commands are blocked per outlet while one is in flight, and a command whose outcome is
  unknown is never retried (app, widgets and tile alike).
* **Languages**: Arabic + English, following the phone by default; override in
  Settings (also appears in Android's per-app language menu).
* Debug build, no signing setup — fine for personal use, not for the Play Store.

---

## Web UI and HTTP API

`GET /` is the web UI. The JSON API is documented in **[API.md](API.md)**. It covers state, on/off,
names, rules, history, settings, events and push tests.

The web UI has five tabs (Outlets, Rules, Energy, Alerts, Settings), Arabic (RTL) and English, and
three themes picked in Settings: *Kraft paper* (default), *White & blue* (follows the device's
light/dark mode) and *Chalkboard*. Its fonts are embedded in `powerk.py` and served at `/fonts/`, so
the page works on a LAN with no internet access.

When `--token` is set, every request needs the token, sent in one of these ways:

* `X-Token: TOKEN` header (what the app sends)
* HTTP Basic, user `powerk` + token as password (what the browser does)
* `?t=TOKEN` query parameter. Opening `/?t=TOKEN` once also sets a cookie, so the page keeps working.

`/powerk.py` and `/powerk.apk` stay public on purpose, so a new phone can always
download the app and a machine without Wi-Fi can always download the provisioner.

| Server option | Meaning |
|---|---|
| `--token T` | require token `T` for the UI/API |
| `--mac AABBCCDDEEFF` | accept only these strip MACs (repeat the option or comma-separate them) |
| `--data DIR` | where `powerk.json` / `powerk.db` live (default: next to `powerk.py`) |
| `--web-port`, `--ip` | web port, and the IP shown in hints |

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
| A schedule ran at the wrong hour | the server's clock or timezone is wrong (VPS: `timedatectl`) |
| No push arrives | Settings → **Send test** shows which channel failed and why |

## What this does not do (on purpose)

MQTT/Home Assistant entities, Matter, firmware OTA, multi-user accounts, and tariff tiers or tax: costs
use one flat price per kWh. Add them when you actually want them.

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


