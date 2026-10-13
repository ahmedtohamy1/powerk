# powerk HTTP API (v2)

JSON over HTTP, served by `powerk.py` on the web port (default 8080).

## Conventions

- **Auth.** When the server runs with `--token`, every `/api/*` call needs it. Send it as the `X-Token`
  header (apps), HTTP Basic with user `powerk` and the token as the password (browsers), the `?t=` query
  parameter, or the `powerk_t` cookie that the web page sets. A missing or wrong token returns
  `401 {"error":"token required"}`.
- **Bodies.** `POST` and `PUT` bodies are JSON objects and need `Content-Type: application/json`;
  otherwise the server returns `415`. `DELETE` takes no body.
- **Errors.** Every error is `{"error":"<human readable>"}` with a 4xx/5xx status code:
  - `400`: bad field or query parameter
  - `401`: no token or wrong token
  - `404`: unknown path, rule or strip
  - `415`: wrong content type
  - `502`: command sent but the outcome is unknown
  - `503`: strip offline
- **Values.**
  - Times are integer Unix seconds and need a 64-bit integer (`Long` in Kotlin).
  - MACs are 12 uppercase hex digits.
  - Booleans must be real JSON booleans; the string `"false"` is rejected.
  - Clients must ignore unknown response fields.
- **Local time.** Server local time defines days, months and schedules. `/api/state` returns
  `server_time` and `utc_offset_seconds` so a client can show times the way the server sees them.
- **Polling.** `/api/state` returns cached state and never waits for the strip, so polling it every few
  seconds is cheap. Load rules, history, settings and events when their screen opens, and again after a
  change.

## GET /api/state

```json
{
  "api_version": 2, "server_time": 1760190000, "utc_offset_seconds": 10800,
  "local_ip": "192.168.1.50", "port": 10086,
  "currency": "EGP", "price_kwh": 2.14, "budget": 500.0,
  "month_kwh": 12.3, "month_cost": 26.32,
  "unread_alerts": 2,
  "devices": [{
    "mac": "AABBCCDDEEFF", "name": "MTTL DDEEFF", "label": "Desk",
    "model": "lgutap", "fw": "0.1.54-1.0.66", "ip": "192.168.1.77",
    "online": true, "reported_at": 1760189998,
    "on": true, "power_w": 61.2, "energy_kwh": 3.21,
    "voltage": 221.7, "current_a": 0.28, "rssi": -56,
    "today_kwh": 0.42, "month_kwh": 12.3, "month_cost": 26.32,
    "alarms": ["temp_high"], "alarm": "temp_high",
    "outlets": [{
      "n": 1, "name": "Fridge", "on": true, "power_w": 61.2, "energy_kwh": 3.2,
      "temp_c": 31,
      "alarms": [], "alarm": null
    }]
  }]
}
```

- **Names.** `name` is the default strip name. `label` is the custom name, or `""` when none is set.
  Show `label` if it is set, otherwise `name`. An outlet `name` of `""` means "Outlet N".
- **`outlets`** always lists outlets 1 to 4. Find an outlet by its `n`, never by its list position.
- **Offline strips.** Strips the server has seen before stay in `devices` with `online: false` after a
  restart. Their readings are the last known values, which may be startup defaults.
  `reported_at` is `null` until the first reading.
- **Alarms.** `alarms` lists active alarms: `temp_high` (outlet temperature ≥ `temp_max_c`) and
  `power_high` (strip total ≥ `power_max_w`; device level only). The strip's own overload/overheat
  fields are protection settings, not trips, so they are not reported. Treat unknown codes as generic
  alarms.
  `alarm` is the first entry of `alarms`, or `null`.
- **Energy totals.**
  - `today_kwh` and `month_kwh` cover the server-local calendar day and month.
  - Top-level `month_kwh` and `month_cost` add up all strips. The monthly budget compares against these.
  - `month_cost` is `month_kwh` × `price_kwh`. It is an estimate, not a bill.

## POST /api/onoff

Request: `{"mac":"AABBCCDDEEFF","outlet":0,"on":true}`. Outlet `0` means all four outlets.

Response: `{"ok":true,"confirmed":true}`.

- `ok: true` means the commands were sent.
- `confirmed` is `true` only when a fresh reading shows every requested outlet in the requested state.
  When `confirmed` is `false`, the outcome is unknown. Never retry an unknown outcome as a toggle.
- Errors: `400` for a bad body, `503` when the strip is offline, `502` when the command was sent but
  the outcome is unknown.
- Allow at least 15 s before a client read timeout. The server can wait up to 10 s.

## POST /api/name

Request: `{"mac":"…","outlet":0,"name":"Desk"}`. Outlet `0` names the strip itself.

- An empty name resets to the default.
- A name can have up to 40 characters.
- The strip must be one the server knows.

Response: `{"ok":true}`.

## Rules

Rules run on the server, so they keep working while the phone is off.

### Rule object

```json
{
  "id": "r_ab12cd", "kind": "schedule", "mac": "AABBCCDDEEFF",
  "outlets": [1, 2, 3, 4], "enabled": true, "label": "",
  "next_at": 1760190000, "phase": null
}
```

The kind-specific fields follow, each with the API values allowed:

| kind | fields |
|---|---|
| `schedule` | `"at":"07:30"` (24 h), `"days":[0..6]` (0 = Monday), `"on":true` |
| `timer` | create with `"minutes":1-10080` and `"on"`. The server stores `"fire_at"` (epoch s). |
| `cycle` | `"on_min":1-1440`, `"off_min":1-1440` |
| `autooff` | `"below_w":0-3500` (W, must be more than 0), `"for_min":1-1440` |

- `outlets` is a non-empty list of numbers from 1 to 4. Sending `[0]` means all four outlets.
- `label` is optional and can have up to 40 characters.
- These fields are computed by the server and are never sent by a client:
  - `id`
  - `next_at`: the next action time as an epoch, or `null` when the rule is disabled or has none pending.
    For an autooff rule, `next_at` is the time the outlet switches off if the low draw continues.
  - `phase`: for a cycle only, the phase the server intends (`"on"` or `"off"`). It is not the observed
    relay state.

### Behavior

- **schedule:** fires once in its local minute, on the listed weekdays. If the strip is offline at that
  time, the server logs `rule_skipped` and does not replay the action later. For an overnight period,
  use two rules.
- **timer:** fires once at `fire_at`, then deletes itself.
  - If the strip is offline, the server waits up to 5 minutes, then logs `rule_skipped`.
  - The timer is consumed before the command is sent, so a crash never replays it.
  - A PUT with `minutes` sets a new deadline from now.
- **cycle:** starts with ON as soon as it is enabled, created or edited, then alternates. After a server
  restart, the cycle starts again with ON.
- **autooff:** checks each listed outlet on its own. When an outlet is ON and draws less than
  `below_w` for `for_min` minutes of fresh readings, the server switches only that outlet off and logs
  `autooff`.

### Endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /api/rules?mac=…` (`mac` optional) | none | `{"rules":[Rule…]}` |
| `POST /api/rules` | a rule without `id`, `next_at` or `phase` | `{"ok":true,"rule":Rule}`, or `400 {"error"}` |
| `PUT /api/rules/<id>` | a partial update, for example `{"enabled":false}` | `{"ok":true,"rule":Rule}`. A missing id returns `404`. |
| `DELETE /api/rules/<id>` | none | `{"ok":true}` |

- `PUT /api/rules/<id>` validates the merged rule. A rule's `kind` cannot change. Editing a rule
  restarts its runtime, so a cycle starts again with ON.
- Sending the same `POST` twice creates two rules. Do not retry it blindly.

## GET /api/history?mac=…&range=day|week|month|year[&outlet=1-4]

`mac` is required and must be a strip the server knows.

| range | covers | buckets |
|---|---|---|
| `day` | the last 24 hours | 24 hourly buckets |
| `week` | 7 local days, including today | daily |
| `month` | 30 local days, including today | daily |
| `year` | 12 calendar months, including this month | monthly |

```json
{"range":"week","unit":"day","currency":"EGP",
 "buckets":[{"t":1760131200,"kwh":0.42,"cost":0.9}],
 "total_kwh":3.1,"total_cost":6.63,
 "per_outlet":[{"n":1,"kwh":1.2},{"n":2,"kwh":0},{"n":3,"kwh":1.9},{"n":4,"kwh":0}]}
```

- `t` is the local start of a bucket. A bucket with no data shows `0`.
- Energy comes from deltas of the strip's own counter.
- While the strip is offline, the energy it used is spread over the hours of the gap when it reconnects.
- Costs use the current flat `price_kwh`.

## Settings

`GET /api/settings`:

```json
{"currency":"EGP","price_kwh":2.14,"budget":500.0,"temp_max_c":60,"power_max_w":3500,
 "ntfy_url":"https://ntfy.sh/my-topic","telegram_chat":"12345","telegram_configured":true}
```

`PUT /api/settings` takes a partial body and returns the same shape as GET.

| field | values |
|---|---|
| `currency` | 1 to 8 characters |
| `price_kwh` | 0 to 1000 |
| `budget` | a monthly amount in the currency, or `null` to switch it off |
| `temp_max_c` | 30 to 120 |
| `power_max_w` | 100 to 10000 |
| `ntfy_url` | an `http(s)` URL, or `""` |
| `telegram_chat` | text |
| `telegram_token` | write-only text: leave it out to keep the saved token, send `""` to clear it, send text to replace it |

Settings apply to the whole server; one budget covers all strips. Unknown fields return `400`.

## Events (alerts and activity)

`GET /api/events?limit=50[&before_id=123]` returns events newest first:

```json
{"events":[{"id":42,"ts":1760190000,"level":"alert","code":"temp_high","mac":"…","outlet":4,
            "text":"Desk / Outlet 4: temp high 61 C","params":{"temp_c":61,"watts":12.0}}],
 "unread_alerts":1}
```

- To page back, use the smallest `id` of the last page as `before_id`.
- A GET does not mark anything read.
- `POST /api/events/read` with `{"through_id":42}` marks events up to that id as read. Leave out
  `through_id` to mark every event read.
  - Response: `{"ok":true,"unread_alerts":0}`.
  - The read position is shared by all clients.
- `unread_alerts` counts only unread `warn` and `alert` events.
- The server keeps the latest 500 events.

| code | level | params |
|---|---|---|
| `offline` | warn | — (sent after the strip has been offline for 60 s) |
| `online` | info | — (sent only after an `offline` alert) |
| `temp_high` | alert | `temp_c`, `watts` (outlet level) |
| `power_high` | alert | `watts` (device level, `outlet` 0) |
| `budget` | warn, or alert at 90 % and above | `pct` (50, 75, 90 or 100), `cost` |
| `rule_fired` | info | `rule_id`, `on`, `outlets` |
| `rule_skipped` | warn | same as `rule_fired`; the strip was offline and nothing was sent |
| `rule_failed` | warn | same as `rule_fired`; the command was sent but not confirmed |
| `autooff` | info | `rule_id`, `watts` |

The server sends every code to the push channels except `rule_fired` and `online`. Clients should show
a translated message for each known `code`, and fall back to `text` for codes they do not know.

## POST /api/alerts/test

Request: `{}`. The server sends a test notification to every configured channel.

Response: `{"ok":true,"sent":["ntfy"],"failed":[{"channel":"telegram","error":"HTTP 401"}]}`.

When no channel is configured, `ok` is `false` and the response also contains
`"error":"no push channel configured"` (status 200). A channel in `sent` only means the provider
accepted the message.

## Public files (no token)

- `GET /powerk.py` downloads the server script.
- `GET /powerk.apk` downloads the debug APK, if it has been built.
