# Security

AzMon is a **personal, local** tool. It holds no credentials of its own — it
reuses your existing `az login` session via `DefaultAzureCredential`.

## Threat model & defaults
- **Binds to `127.0.0.1` by default** (this machine only). Nothing is exposed
  to your network unless you opt in.
- **LAN / phone access is opt-in and gated.** To reach it from another device,
  set `web.host: "0.0.0.0"` **and** a strong `web.auth_token` in `config.yaml`.
  Open `http://<pc-ip>:8000/?token=YOURTOKEN` once; a cookie keeps you in.
  Without the token every request returns `401`.
- **Secrets stay out of git.** `config.yaml` (ntfy/telegram/auth tokens) is
  git-ignored; commit only `config.example.yaml`. The local SQLite cache
  (`data/`) is git-ignored too.

## Data handling
- The local SQLite DB caches alert metadata (resource/rule names, severities,
  timestamps) for a rolling window (default 24h) and is **not encrypted at
  rest**. It stores **no credentials**. Treat it as sensitive if your resource
  names are.
- `/api/account` surfaces your signed-in Azure account name/tenant to the local
  UI only — never persisted, never sent anywhere.
- Read-only against Azure: only `GET` calls to `management.azure.com`. AzMon
  never modifies Azure resources or alert state.

## Hardening notes
- All SQL is parameterized; config is parsed with `yaml.safe_load`.
- UI escapes all Azure-supplied strings before rendering (XSS-safe); external
  links use `rel="noopener"`.
- Run `pip-audit` (or `safety`) against `requirements.txt` before releases.

## Reporting
This is a hobby/open-source project — open an issue for suspected problems.
Do not include real subscription IDs, resource names, tenant IDs, or emails.
