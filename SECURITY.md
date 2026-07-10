# Security

AzMon is a **personal, local** tool. It holds no credentials of its own — it
reuses your existing `az login` session via `DefaultAzureCredential`, and it
only ever makes read (`GET`) calls against Azure.

## Scope of access
- **ARM** (`management.azure.com`): lists alerts, metrics, and (if you check
  PIM status) role assignment schedules. Whatever your signed-in account can
  already read in the Azure portal — nothing more, nothing less. No write
  calls are ever made; AzMon cannot change an alert's state or any resource.
- **Log Analytics** (`api.loganalytics.io`): only used for the optional KQL
  drill-down on scheduled-query alerts, and only re-runs a query the alert
  rule itself already defines.
- No other external endpoints are contacted unless *you* enable a
  notification sender (see [Third-party senders](#third-party-senders-opt-in)
  below) — there is no telemetry, analytics, or update-check phoning home.

## Threat model & defaults
- **Binds to `127.0.0.1` by default** (this machine only). Nothing is exposed
  to your network unless you opt in.
- **LAN / phone access is opt-in and gated.** To reach it from another device,
  set `web.host: "0.0.0.0"` **and** a strong `web.auth_token` in `config.yaml`.
  Open `http://<pc-ip>:8000/?token=YOURTOKEN` once; a cookie keeps you signed
  in. Without the token, every request gets `401`.
- **Secrets stay out of git.** `config.yaml` (which holds ntfy/telegram/
  WhatsApp/auth tokens) is git-ignored; only `config.example.yaml` (all
  placeholder values) is committed. The local SQLite cache (`data/`) is
  git-ignored too.

## Data handling
- The local SQLite DB caches alert metadata (resource/rule names, severities,
  timestamps) for a rolling window (`lookback` in config, default 24h) and is
  **not encrypted at rest**. It stores **no credentials**. Treat it — and any
  backup of it — as sensitive if your resource/rule names are, and consider
  full-disk encryption on machines that leave your control.
- `/api/account` surfaces your signed-in Azure account name/tenant to the
  local UI only — never persisted to disk, never sent anywhere else.
- Everything above is local to the machine you run AzMon on. There is no
  shared backend between instances, even if several people in the same org
  each run their own copy.

## Third-party senders (opt-in)
Every notification channel other than the in-page banner is **off by
default**; you explicitly enable each one in `config.yaml`. When enabled,
here's exactly what leaves your machine, and to whom:

| Sender | Receives | Sent to |
|---|---|---|
| Windows toast | nothing — stays on-device | n/a (local OS API) |
| ntfy | severity, rule name, resource/group name, alert count, fired time | your ntfy server (ntfy.sh by default, or self-hosted) |
| Telegram | same fields as above | Telegram's API, routed to your bot/chat |
| WhatsApp | same fields as above | CallMeBot (third-party free relay), which forwards to WhatsApp |

No alert **description**, KQL **query results**, or subscription/tenant ID is
ever included in a notification payload — only the short summary line shown
in the dashboard's own toast/beep. Still, rule and resource *names* are
visible to whichever of these third parties you opt into, so pick topic
names/tokens that don't leak anything you'd rather keep private (e.g. an
unguessable ntfy topic), and skip a channel entirely if your resource naming
itself is sensitive.

## Hardening notes
- All SQL is parameterized; config is parsed with `yaml.safe_load` (never
  `yaml.load`).
- The UI escapes all Azure-supplied strings before rendering (XSS-safe);
  external links use `rel="noopener"`.
- Run `pip-audit` (or `safety`) against `requirements.txt` before releases —
  CI does this on every push as a non-blocking check.

## Reporting a vulnerability
This is a hobby/open-source project maintained in spare time, with no formal
SLA — but real reports are taken seriously. Please **do not open a public
issue** for a suspected vulnerability; instead use GitHub's private
**[Security Advisories](../../security/advisories/new)** on this repo, or
open a normal issue asking for a private channel if that's unavailable to
you. Either way, do not include real subscription IDs, tenant IDs, resource
names, tokens, or email addresses in the report itself — describe the issue
and, if needed, share sensitive specifics privately once a channel is open.
