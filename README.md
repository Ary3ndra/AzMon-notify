# azmon-notify

[![CI](https://github.com/Ary3ndra/AzMon-notify/actions/workflows/ci.yml/badge.svg)](https://github.com/Ary3ndra/AzMon-notify/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A free, local Azure Monitor alert console. No Azure resources to deploy, no
Logic Apps, no Action Groups, no per-notification cost. It polls the Alerts
Management REST API with **your own `az login` session**, keeps a rolling
local cache, and shows everything on a live dashboard you can open from your
laptop — and, optionally, your phone on the same network — plus native
desktop toasts and push notifications.

Run it as one person watching your own subscriptions, or as a team each
running their own instance against a shared tenant. There's no server
component, no shared database, and no telemetry — everything lives on the
machine you start it on.

## Contents
- [Why it's free](#why-its-free)
- [Features](#features)
- [Getting started](#getting-started)
- [Reaching it from your phone / LAN](#reaching-it-from-your-phone--lan)
- [Notifications](#notifications)
- [Configuration](#configuration-configyaml)
- [Extending it (plugins)](#extending-it-plugins)
- [Layout](#layout)
- [Troubleshooting](#troubleshooting)
- [Security & privacy](#security--privacy)
- [Honest limits](#honest-limits)

## Why it's free
- Reads alerts via `GET .../Microsoft.AlertsManagement/alerts`. Plain ARM
  read calls cost nothing.
- Auth is your existing Azure CLI session (`DefaultAzureCredential`). **Reader**
  role is enough — if you can already see an alert in the Azure portal, you
  already have everything this needs. No service principal, no app
  registration, no secrets to provision.
- Delivery is local: a web UI, OS-native toast, and optional push channels you
  already own (ntfy, Telegram, WhatsApp).

## Features
- **Live dashboard** — severity tiles, Active/Closed tabs, smart grouping
  (by rule, with all Service Health alerts collapsed into one bucket), or
  group by resource / resource group / severity / signal type / subscription.
  Sort by newest, oldest, severity, name, or resource. Text search, timeframe
  filter, collapse-all, light/dark theme, all persisted per-browser.
- **Live updates** over Server-Sent Events — no polling, no refresh. New
  alerts flash, beep (toggle-able), and the header pulses by worst severity.
- **Mute / snooze per rule** — 1h / 8h / 1d / forever, with a visible MUTED
  badge; muted groups sink to the bottom instead of disappearing.
- **Dismiss / undismiss**, single or all, with a hint banner and an accurate
  dismissed count in the summary.
- **Metrics drill-down** — pull the same Azure Monitor Metrics graphs the
  portal shows (CPU, memory, etc.), unit-aware charts, metric picker, no
  extra auth scope beyond ARM.
- **KQL drill-down** — for scheduled-query alerts, re-run the alert's own KQL
  against Log Analytics and see the result set inline.
- **Quiet hours** — suppress toasts/push/beep in a configurable window; the
  dashboard still updates silently.
- **Settings menu** — poll now, purge old alerts, edit `config.yaml` from the
  UI (validated, hot-reloaded — no restart for most changes), view live
  backend logs, re-authenticate (`az login`) without leaving the browser,
  send a test notification, export alerts as JSON/CSV.
- **PIM awareness** — flags when you're acting against a subscription where
  your role is only active via Privileged Identity Management, so you don't
  forget it'll expire.
- **Plugins** — drop a Python file in `azmon_notify/plugins/`, subclass
  `Plugin`, get an `on_new_alerts` hook and/or your own `/api/ext/...` routes.
  Toggle plugins on/off live from the UI. See
  [azmon_notify/plugins/README.md](azmon_notify/plugins/README.md).
- **Headless mode** — just the poller + notifications, no web server, for
  running on a machine you don't want a browser open on.

## Getting started

### Windows (recommended path)
**One-time setup**
1. Install [Python 3.11+](https://www.python.org/downloads/) (tick *Add to
   PATH*) and the [Azure CLI](https://aka.ms/installazurecli).
2. Open a terminal and run `az login`, signing in with the account that
   already sees these alerts in the Azure portal (Reader role is enough).
3. *(Optional)* Copy `config.example.yaml` to `config.yaml` and edit it — the
   defaults work out of the box. See [Configuration](#configuration-configyaml)
   to scope it to specific subscriptions, change the poll interval, or turn
   on phone push.

**Every time you want the console**

Run **`run.ps1`** — it's a script, it must be *executed*, not opened.
Double-clicking it inside an editor just opens the text for editing; that's
not running it.

- **Terminal (recommended)** — open a PowerShell terminal in the project
  folder (VS Code: `` Ctrl+` `` opens one already there) and run:
  ```powershell
  .\run.ps1
  ```
- **Explorer, no terminal** — right-click `run.ps1` in Windows File Explorer
  → **Run with PowerShell**. (Plain double-click on a `.ps1` opens it in an
  editor by Windows' own default — that's a Microsoft safety default, not a
  bug in this script.)

If PowerShell refuses with *"running scripts is disabled on this system"*,
your execution policy is blocking it. Either use **Run with PowerShell**
above (bypasses it for a single right-click run), or once, in a terminal:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

What happens next:
1. First run only: it creates a `.venv`, installs everything in
   `requirements.txt`, and checks `az account show` (re-runs `az login` if
   your session expired).
2. It prints two URLs and starts the server — leave the window open, it *is*
   the running app:
   ```
   Console:  http://localhost:8000
   Phone:    http://192.168.x.x:8000   (same Wi-Fi)
   ```
3. Open the **Console** URL in a browser on the same PC.
4. On the very first run the dashboard silently fills with whatever is
   currently fired (no toast/push spam for old alerts) — that's expected.
   From then on, only genuinely new alerts trigger notifications.

### Linux / macOS (or anyone who'd rather skip the script)
The app itself is plain Python + FastAPI — nothing here is Windows-only
except the optional native-toast sender and the `.ps1` launchers.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
az login
cp config.example.yaml config.yaml   # edit as needed
python -m azmon_notify.web.app        # dashboard, http://localhost:8000
# or, for the notify-only headless mode:
python -m azmon_notify.main -c config.yaml
```

### Stopping it
Close the console window, or click into it and press `Ctrl+C`. It's a local
process — nothing keeps running in the background, and nothing in Azure
changes (the app is read-only against the Alerts API).

### Restarting after a config change
Most settings can be edited live from the in-app **Settings → Edit config**
menu and take effect immediately (poll interval, severity/state filters,
senders, plugins enable/disable). Changing `web.host` / `web.port` still
needs a restart.

## Reaching it from your phone / LAN
By default AzMon binds to `127.0.0.1` (this PC only) for safety. To use it
from another device on your network:
1. In `config.yaml`, set `web.host: "0.0.0.0"` **and** a strong
   `web.auth_token`.
2. Open `http://<pc-ip>:8000/?token=YOURTOKEN` once from the other device — a
   cookie keeps it signed in. Without the token, every LAN request gets `401`.
3. You may need to allow Python through the **Private network** Windows
   Firewall prompt. A locked-down corporate/guest Wi-Fi may still block
   inbound LAN traffic entirely — use `ntfy` as the phone-push fallback in
   that case.

See [Security & privacy](#security--privacy) for the full threat model.

## Notifications
| Channel | Works when | Note |
|---|---|---|
| In-page sound + flash | tab open, any network | no setup |
| Browser notification | localhost or HTTPS only | blocked over plain-http LAN by browsers |
| Windows toast | app runs on your Windows machine | `winotify`, installed automatically by `run.ps1` |
| **ntfy** | anywhere, phone or desktop | install the ntfy app, subscribe to your own unguessable topic |
| Telegram | anywhere | bot token + chat id, see [NOTIFICATIONS.md](NOTIFICATIONS.md) |
| WhatsApp | anywhere | free via CallMeBot, see [NOTIFICATIONS.md](NOTIFICATIONS.md) |

The dashboard is the live board; ntfy/Telegram/WhatsApp are the off-screen
tap on the shoulder when the tab isn't open. Full step-by-step setup for each
channel is in [NOTIFICATIONS.md](NOTIFICATIONS.md).

## Configuration (`config.yaml`)
Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` (git-ignored,
never committed) and edit — every key is documented inline. The highlights:

- `min_severity` (default `4`, i.e. everything) — raise to `3` to mute
  Verbose noise, `2` to also mute Informational.
- `poll_interval_seconds` (default `30`) — the floor on "instant" is Azure's
  own alert evaluation latency (1–5 min for metric alerts), not the poller.
- `lookback` — query window *and* local retention window; bigger is a safer
  catch-up after downtime (dedup means you never get double-fired).
- `subscriptions: []` — auto-discovers every subscription you can read;
  restrict to a list of subscription IDs if you only care about some.
- `group_by`, `quiet_hours`, `senders`, `plugins`, `web` — all documented in
  the example file.

## Extending it (plugins)
Drop a Python module in `azmon_notify/plugins/`, subclass `Plugin`, register
it, flip it on in config (or from the UI's Extensions list — no config edit
needed, and no `.py` upload requires a restart). A plugin can react to new
alerts and/or expose its own `/api/ext/<name>/...` routes. Full guide:
[azmon_notify/plugins/README.md](azmon_notify/plugins/README.md).

## Layout
```
azmon_notify/
  azure_client.py   token acquisition + alerts/metrics/logs REST + pagination
  poller.py         poll engine -> store + senders
  store.py          live in-memory snapshot for the UI (grouping, sorting)
  state.py          sqlite dedup, ack, mute state
  grouping.py       bucket by resource/severity/rule
  config.py         load + validate config.yaml
  senders/          windows_toast, ntfy, telegram, whatsapp (add your own)
  plugins/          drop-in extensions, see plugins/README.md
  web/
    app.py          FastAPI: REST API + SSE + serves the dashboard
    static/          index.html, styles.css, app.js, vendored uPlot
config.example.yaml   template — copy to config.yaml (git-ignored)
requirements.txt · requirements-dev.txt
run.ps1 · run_headless.ps1   (Windows one-click launchers)
tests/                20+ tests covering the core (pytest)
```

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Double-clicking `run.ps1` in an editor "does nothing" | That opens the file for *editing*, it never executes. Run it from a terminal (`.\run.ps1`) or, in real Windows Explorer, right-click `run.ps1` → **Run with PowerShell**. |
| `running scripts is disabled on this system` | Execution policy is blocking `.ps1`. Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use right-click → Run with PowerShell, which bypasses it. |
| Script exits with "Azure CLI not found" | Install the Azure CLI, reopen the terminal so PATH updates, retry. |
| Dashboard loads but stays empty forever | Check the console log for `403 on sub ... — skipping` — you lack Reader on that subscription; or your account just has no fired alerts right now (compare against the Azure portal's Alerts blade). |
| `az login` window pops up every launch | Your CLI token expired (typical after ~90 min–24h depending on tenant policy); sign in again — the app detects it via `az account show`, or use **Re-authenticate** in the Settings menu. |
| Phone can't reach `http://PC-IP:8000` | Windows Firewall prompt was dismissed/blocked — allow Python on Private networks — or the machine is on a managed/corporate Wi-Fi profile that blocks inbound LAN traffic; fall back to `ntfy` for phone push. |
| Phone URL shows a weird IP like `172.x.x.x` or `169.254.x.x` | That's a virtual adapter (Hyper-V/VPN/WSL), not your real Wi-Fi IP. `run.ps1` picks the adapter with a default gateway to avoid this; if it's still wrong, run `ipconfig`/`ip addr` and use the address on your actual Wi-Fi/Ethernet adapter. |
| Port 8000 already in use | Change `web.port` in `config.yaml` and restart. |
| No Windows toasts | Confirm `senders.windows_toast.enabled: true` in `config.yaml` and that `winotify` installed (it's platform-conditional in `requirements.txt`; check the console log for a sender init error). |
| Toasts/push fire for old alerts on first launch | Set `notify_on_first_run: true` in `config.yaml` if you actually want that (default is silent seeding, by design). |

## Security & privacy
- Nothing here holds credentials of its own — it reuses your `az login`
  session. Binds to `127.0.0.1` by default; LAN access is opt-in and gated by
  a shared token (see [above](#reaching-it-from-your-phone--lan)).
- Read-only against Azure: only `GET` calls against `management.azure.com`
  (and `api.loganalytics.io` for the optional KQL drill-down). It never
  modifies Azure resources or alert state.
- No telemetry, no external services beyond Azure and whichever notification
  channels you explicitly enable.
- The local SQLite cache holds alert metadata (resource/rule names,
  severities, timestamps) for a rolling window, not encrypted at rest —
  treat `data/` as sensitive if your resource names are, and consider
  full-disk encryption on shared machines.
- Full threat model, hardening notes, and how to report an issue: see
  [SECURITY.md](SECURITY.md).

## Honest limits
- This is a personal console per machine, not a shared alerting backend —
  each person/team member who wants it runs their own instance against the
  subscriptions they can already read. It doesn't replace org-wide alerting
  infrastructure; it just stops *you* from flying blind, for free.
- Local ack/dismiss is UI-only; it doesn't change the alert's state in Azure
  (that would need write permissions). Left as a deliberate, safe default.
- Browser push notifications need a secure context; over plain-http LAN
  they're off by design — sound + flash + ntfy/Telegram/WhatsApp cover it.

## License
[MIT](LICENSE).
