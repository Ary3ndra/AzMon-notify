# azmon-notify

A personal Azure Monitor alert console. No Azure resources, no Power Automate,
no Logic App, no cost. It polls the Alerts Management REST API with your own
`az login` token, then shows everything on a Material dashboard you can open
from your laptop **and your phone** on the same Wi-Fi — plus native Windows
toasts and optional phone push.

## Why it's free
- Reads alerts via `GET .../Microsoft.AlertsManagement/alerts`. ARM API calls
  cost nothing.
- Auth is your existing Azure CLI session (Reader is enough — you already see
  these alerts, so you already have it). No service principal, no app reg.
- Delivery is local: a web UI, Windows toast, and optional ntfy/Telegram.

## Getting started (Windows 11)

**One-time setup**
1. Install [Python 3.11+](https://www.python.org/downloads/) (tick *Add to PATH*)
   and the [Azure CLI](https://aka.ms/installazurecli).
2. Open a terminal and run `az login`, sign in with the account that already
   sees these alerts in the Azure portal (Reader role is enough).
3. (Optional) Edit `config.yaml` — the defaults work out of the box, but see
   [Tune it](#tune-it-configyaml) below if you want to scope it to specific
   subscriptions, change the poll interval, or turn on phone push.

**Every time you want the console**

Run **`run.ps1`** — this is a script, it must be *executed*, not opened.
Double-clicking it inside VS Code (or any editor) just opens the text for
editing and does nothing else; that's not running it. Pick one:

- **Terminal (recommended)** — open a PowerShell terminal in the project
  folder (VS Code: `` Ctrl+` `` opens one already there) and run:
  ```powershell
  .\run.ps1
  ```
- **Explorer, no terminal** — right-click `run.ps1` in Windows File Explorer
  → **Run with PowerShell**. (Plain double-click on a `.ps1` opens it in an
  editor too, by Windows' own default — this is a Microsoft safety default,
  not a bug in this script.)

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
2. It prints two URLs and then starts the server — leave the window open,
   it *is* the running app:
   ```
   Console:  http://localhost:8000
   Phone:    http://192.168.x.x:8000   (same Wi-Fi)
   ```
3. Open the **Console** URL in a browser on the same PC.
4. On the very first run the dashboard silently fills with whatever is
   currently fired (no toast/ntfy spam for old alerts) — that's expected.
   From then on, only genuinely new alerts trigger notifications.

> **Reaching it from your phone (opt-in).** By default AzMon binds to
> `127.0.0.1` (this PC only) for safety. To use it from another device on your
> Wi-Fi, set in `config.yaml`: `web.host: "0.0.0.0"` **and** a strong
> `web.auth_token`, then open `http://<pc-ip>:8000/?token=YOURTOKEN` once (a
> cookie keeps you signed in). Without the token, LAN requests are rejected.
> You may also need to allow Python on **Private** networks at the Windows
> Firewall prompt; a locked-down corporate Wi-Fi may still block it — use
> `ntfy` (below) as the phone-push fallback.

A `run.bat` equivalent is still included if you genuinely prefer plain
double-click-to-run in Windows Explorer (`.bat` files execute on double-click
by default, unlike `.ps1`) — same behavior, same prerequisites.

### Stopping it
Close the console window, or click into it and press `Ctrl+C`. It's a local
process — nothing keeps running in the background, and nothing in Azure
changes (the app is read-only against the Alerts API).

### Restarting after a config change
`config.yaml` is only read at startup. After editing it (subscriptions,
`min_severity`, senders, etc.), stop and re-run `run.ps1` to pick it up.

## The dashboard
- Severity tiles (Active / Critical / Error / Warning / Info / Verbose) — click
  one to filter. Mirrors the Azure portal view.
- Alerts grouped by resource, worst severity first, collapsible.
- Live: updates stream over Server-Sent Events, no refresh. New alerts flash,
  beep (toggle **Sound on**), and the header pulses by worst severity.
- Dismiss single alerts or all; **Show dismissed** to bring them back.

## Notifications, honestly
| Channel | Works when | Note |
|---|---|---|
| In-page sound + flash | tab open, any network | no setup |
| Windows toast | app runs on your laptop | `winotify`, installed by run.ps1 |
| Browser notification | localhost or HTTPS only | blocked on plain-http LAN by browsers |
| **ntfy** (phone push) | anywhere | install ntfy app, subscribe to your topic |
| Telegram | anywhere | bot token + chat id |

For real **phone** push, turn on `ntfy` in `config.yaml` (use ntfy.sh free with
an unguessable topic, or self-host on a Pi later). The dashboard is the live
board; ntfy is the off-screen tap on the shoulder.

## Tune it (`config.yaml`)
- `min_severity: 3` (default) — mutes Sev4/Verbose noise, keeps
  Critical/Error/Warning/Informational. Set `2` to also mute Informational.
- `poll_interval_seconds` — 30 default. Floor on "instant" is Azure's own eval
  latency (metric alerts 1–5 min), not the poller.
- `lookback` — query window; bigger = safer catch-up after downtime (dedup
  absorbs the overlap so you never double-fire).
- `group_by`, `subscriptions`, channels — all in the file.

## Headless mode
Just toasts/ntfy/telegram, no web: `.\run_headless.ps1` (or `run_headless.bat`,
or directly `.venv\Scripts\python.exe -m azmon_notify.main -c config.yaml`).

## Layout
```
azmon_notify/
  azure_client.py   token + alerts REST + pagination
  poller.py         poll engine -> store + senders
  store.py          live in-memory snapshot for the UI
  state.py          sqlite dedup + ack
  grouping.py       bucket by resource/severity/rule
  senders/          windows_toast, ntfy, telegram  (add your own)
  web/
    app.py          FastAPI: API + SSE + serves the dashboard
    static/         index.html, styles.css, app.js
config.yaml · requirements.txt
run.ps1 · run_headless.ps1   (recommended)
run.bat · run_headless.bat   (Explorer double-click alternative)
```

## Add a channel (room for features)
Subclass `Notifier`, `@register("name")`, implement `send_group`, enable in
config. Quiet-hours / per-severity routing / Teams-via-Graph fit as wrapper
Notifiers — stubs noted in `senders/channels.py`.

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Double-clicking `run.ps1`/`run.bat` in VS Code "does nothing" | That opens the file for *editing*, it never executes. Run it from a terminal (`.\run.ps1`) or, in real Windows Explorer, right-click `run.ps1` → **Run with PowerShell** (or double-click `run.bat`, which Explorer does execute). |
| `running scripts is disabled on this system` | Execution policy is blocking `.ps1`. Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use right-click → Run with PowerShell, which bypasses it. |
| Script exits with "Azure CLI not found" | Install the Azure CLI, reopen the terminal so PATH updates, retry. |
| Dashboard loads but stays empty forever | Check the console log for `403 on sub ... — skipping` — you lack Reader on that subscription; or your account just has no fired alerts right now (try the Azure portal's Alerts blade to compare). |
| `az login` window pops up every launch | Your CLI token expired (typical after ~90 min–24h depending on tenant policy); just sign in again, the script detects it via `az account show`. |
| Phone can't reach `http://PC-IP:8000` | Windows Firewall prompt was dismissed/blocked — allow Python on Private networks, or the laptop is on a managed/corporate Wi-Fi profile that blocks inbound LAN traffic; fall back to `ntfy` for phone push. |
| Phone URL shows a weird IP like `172.x.x.x` or `169.254.x.x` | That's a virtual adapter (Hyper-V/VPN/WSL), not your real Wi-Fi IP. `run.ps1` picks the adapter with a default gateway to avoid this; if it's still wrong, run `ipconfig` and use the IPv4 under your actual Wi-Fi/Ethernet adapter. |
| Port 8000 already in use | Change `web.port` in `config.yaml` and restart. |
| No Windows toasts | Confirm `senders.windows_toast.enabled: true` in `config.yaml` and that `winotify` installed (it's in `requirements.txt`; check the console log for a sender init error). |
| Toasts/ntfy fire for old alerts on first launch | Set `notify_on_first_run: true` in `config.yaml` if you actually want that (default is silent seeding, by design). |

## Honest limits
- This watches **you**. It doesn't fix the team relying on bare Azure Monitor —
  it just stops you flying blind, for free.
- Local ack is UI-only; it doesn't change the alert's state in Azure (that needs
  write perms). Left as a deliberate, safe default.
- Browser push needs a secure context; over LAN http it's off by design —
  sound + flash + ntfy cover it.
