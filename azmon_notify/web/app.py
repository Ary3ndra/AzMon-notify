"""Local web console. Serves the Material dashboard, exposes a small API, and
pushes live updates over Server-Sent Events. Binds to 127.0.0.1 by default
(this PC only); set web.host 0.0.0.0 + web.auth_token to reach it from a phone
on the LAN behind a shared-secret gate.
"""
from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from ..config import ConfigError, load_cfg, validate_cfg
from ..constants import SEV_ORD
from ..plugins import PluginContext, build_plugins
from ..plugins import base as plugbase
from ..plugins import discover as plugin_discover
from ..poller import Poller, _LOOKBACK_HOURS
from ..senders import build_senders
from ..state import State
from ..store import AlertStore

log = logging.getLogger("azmon.web")

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "static")


CFG_PATH = os.environ.get("AZMON_CONFIG", "config.yaml")
cfg = validate_cfg(load_cfg(CFG_PATH))
logging.basicConfig(
    level=getattr(logging, cfg.get("log_level", "INFO")),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ── in-memory log ring buffer (for the UI's log viewer) ──────────────────
_LOGBUF: collections.deque = collections.deque(maxlen=800)


class _BufHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOGBUF.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


_bh = _BufHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_bh)

# ── tiny per-IP rate limiter for the Azure-proxy endpoints ───────────────
_RL: dict = {}


def _ratelimited(key: str, limit: int = 25, per: float = 10.0) -> bool:
    now = time.time()
    q = _RL.setdefault(key, [])
    while q and q[0] < now - per:
        q.pop(0)
    if len(q) >= limit:
        return True
    q.append(now)
    return False

# plugin enable/disable overrides (set from the UI) live in a sidecar so we
# never rewrite the hand-formatted config.yaml. Applied before the poller
# builds its plugin list.
_POV_PATH = os.path.join(
    os.path.dirname(os.path.expanduser(cfg["state_db"])) or ".", "plugin_overrides.json")


def _save_plugin_override(name: str, enabled: bool) -> None:
    try:
        pov = json.load(open(_POV_PATH)) if os.path.exists(_POV_PATH) else {}
    except Exception:  # noqa: BLE001
        pov = {}
    pov[name] = bool(enabled)
    try:
        with open(_POV_PATH, "w") as f:
            json.dump(pov, f)
    except Exception as ex:  # noqa: BLE001
        log.error("save plugin override failed: %s", ex)


try:
    _pov = json.load(open(_POV_PATH)) if os.path.exists(_POV_PATH) else {}
    _pblock = cfg.setdefault("plugins", {})
    for _n, _en in _pov.items():
        _pblock.setdefault(_n, {})["enabled"] = bool(_en)
except Exception:  # noqa: BLE001
    pass

state = State(cfg["state_db"])
store = AlertStore(state)
poller = Poller(cfg, store, state)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_poll_loop())
    log.info("console up — open http://<this-pc-ip>:%s on your phone",
             cfg.get("web", {}).get("port", 8000))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="azmon-notify", lifespan=lifespan)

# ── plugins: let enabled extensions mount their own endpoints ────────────
# Built inside the poller; registered here (before serving) so a plugin can
# expose /api/ext/<name>/... routes. Failures are isolated per plugin.
for _plugin in poller.plugins:
    try:
        _plugin.register_routes(app)
    except Exception as _ex:  # noqa: BLE001
        log.error("plugin '%s' route registration failed: %s",
                  getattr(_plugin, "name", "?"), _ex)

# ── SSE broadcast ───────────────────────────────────────────────────────
_subscribers: set[asyncio.Queue] = set()


async def _broadcast(event: str, data: dict) -> None:
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait((event, data))
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


async def _poll_loop() -> None:
    while True:
        try:
            new = await asyncio.to_thread(poller.poll_once)
            store.touch()
            await _broadcast("summary", store.summary())
            if new:
                await _broadcast("new", {
                    "count": len(new),
                    "worst": min(SEV_ORD.get(a.sev_label, 5) for a in new),
                    "items": [a.to_dict() for a in new],
                })
        except Exception as ex:  # noqa: BLE001
            log.error("poll loop error: %s", ex)
        # read interval each cycle so a config edit applies without a restart
        await asyncio.sleep(cfg.get("poll_interval_seconds", 30))


# ── signed-in Azure account, for the topbar (cached — `az account show`
# spawns a process, no need to do that on every request) ──────────────────
_ACCOUNT_TTL = 300
_account_cache: dict = {"data": None, "ts": 0.0}


def _fetch_account() -> dict:
    try:
        r = subprocess.run(
            ["az", "account", "show", "-o", "json"],
            capture_output=True, text=True, timeout=15, shell=(os.name == "nt"))
        if r.returncode != 0:
            return {"logged_in": False, "name": None, "tenant": None, "subscription": None}
        info = json.loads(r.stdout)
        return {
            "logged_in": True,
            "name": info.get("user", {}).get("name"),
            "tenant": info.get("tenantId"),
            "subscription": info.get("name"),
        }
    except Exception as ex:  # noqa: BLE001
        log.warning("az account show failed: %s", ex)
        return {"logged_in": False, "name": None, "tenant": None, "subscription": None}


def _account() -> dict:
    now = time.time()
    if _account_cache["data"] is None or now - _account_cache["ts"] > _ACCOUNT_TTL:
        _account_cache["data"] = _fetch_account()
        _account_cache["ts"] = now
    return _account_cache["data"]


# ── API ─────────────────────────────────────────────────────────────────
@app.get("/api/summary")
async def api_summary():
    return store.summary()


@app.get("/api/account")
async def api_account():
    return await asyncio.to_thread(_account)


@app.get("/api/plugins")
async def api_plugins():
    """Active extensions, for the UI's extensions indicator."""
    return {"plugins": [{"name": p.name,
                         "description": getattr(p, "description", "")}
                        for p in poller.plugins]}


@app.get("/api/subscriptions")
async def api_subscriptions():
    """subscription id -> display name (filled after the first poll's
    discovery). Lets the UI show a readable sub name instead of a GUID."""
    return poller.az.sub_names


@app.get("/api/alerts")
async def api_alerts(section: str = "open", severities: str = "", q: str = "",
                     show_acked: bool = False, sort: str = "fired_desc",
                     group_by: str = "", within_hours: float = 0):
    sev_set = {s for s in severities.split(",") if s} or None
    return store.query(section=section, severities=sev_set, text=q,
                       show_acked=show_acked, sort=sort,
                       group_by=group_by or cfg["group_by"],
                       within_hours=within_hours or None)


@app.post("/api/ack/{alert_id}")
async def api_ack(alert_id: str):
    state.ack(alert_id)
    await _broadcast("summary", store.summary())
    return {"ok": True}


@app.post("/api/ack_all")
async def api_ack_all():
    for a in store.query(section="open", show_acked=False, group_by="none"):
        state.ack(a["id"])
    await _broadcast("summary", store.summary())
    return {"ok": True}


@app.post("/api/unack/{alert_id}")
async def api_unack(alert_id: str):
    """Restore a single dismissed alert to the Active view."""
    state.unack(alert_id)
    await _broadcast("summary", store.summary())
    return {"ok": True}


@app.post("/api/unack_all")
async def api_unack_all():
    """Clear all dismissals — brings every dismissed alert back."""
    n = state.unack_all()
    await _broadcast("summary", store.summary())
    return {"ok": True, "restored": n}


@app.post("/api/mute")
async def api_mute(key: str, hours: float = 0):
    """Mute a rule/group key (hours=0 => forever, else snooze that long).
    Muted rules stop notifying and sink to the bottom of the list."""
    state.mute(key, hours)
    return {"ok": True}


@app.post("/api/unmute")
async def api_unmute(key: str):
    state.unmute(key)
    return {"ok": True}


@app.get("/api/mutes")
async def api_mutes():
    return state.muted_map()


# ── settings menu actions ────────────────────────────────────────────────
@app.post("/api/purge")
async def api_purge(older_than_hours: float = 0):
    """Clear old alerts. older_than_hours=0 wipes the whole DB."""
    n = state.purge(older_than_hours)
    await _broadcast("summary", store.summary())
    return {"ok": True, "removed": n}


@app.post("/api/poll_now")
async def api_poll_now():
    """Force an immediate poll instead of waiting for the interval."""
    new = await asyncio.to_thread(poller.poll_once)
    store.touch()
    await _broadcast("summary", store.summary())
    return {"ok": True, "new": len(new)}


@app.get("/api/status")
async def api_status():
    """DB + config facts for the settings panel."""
    c = state.counts()
    return {
        "db": c,
        "lookback": cfg.get("lookback"),
        "retention_hours": _LOOKBACK_HOURS.get(cfg.get("lookback"), 24),
        "poll_interval_seconds": cfg.get("poll_interval_seconds"),
        "min_severity": cfg.get("min_severity"),
        "subscriptions": len(poller.subs),
        "last_updated": store.last_updated,
    }


@app.get("/api/export")
async def api_export(section: str = "open", fmt: str = "json"):
    """Download the current alert list as JSON or CSV."""
    rows = store.query(section=section, show_acked=True, group_by="none")
    cols = ["sev_label", "alert_kind", "rule_name", "resource_name",
            "resource_group", "subscription", "condition", "fired_at"]
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for a in rows:
            w.writerow([a.get(c, "") for c in cols])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="azmon-{section}.csv"'})
    return JSONResponse(rows, headers={"Content-Disposition":
                        f'attachment; filename="azmon-{section}.json"'})


@app.post("/api/test_notify")
async def api_test_notify():
    """Fire a sample notification through every enabled sender."""
    from ..azure_client import Alert
    sample = Alert(
        id="test", rule="AzMon/Test-Alert", severity="Sev2", sev_label="Warning",
        state="New", condition="Fired", signal_type="Test", target_resource="",
        target_resource_name="test-resource", target_resource_group="test-rg",
        fired_at="", description="This is a test notification from AzMon.",
        subscription="", monitor_service="Test")
    await asyncio.to_thread(poller._notify, [sample])
    return {"ok": True, "senders": len(poller.senders)}


# ── resource metrics (Azure Monitor) ─────────────────────────────────────
import re as _re  # noqa: E402
_SUB_RE = _re.compile(r"^/subscriptions/([0-9a-fA-F-]{36})/")


def _valid_resource(rid: str) -> bool:
    """Guard: must be a well-formed ARM resource id (no path-traversal / host
    tricks — we only ever splice it into a management.azure.com metrics URL).
    We also prefer it to be under a subscription we watch, but only once
    discovery has run — otherwise metrics clicked right after launch would
    race the first poll and get rejected. The user's own token still limits
    what can actually be read."""
    rid = rid or ""
    m = _SUB_RE.match(rid)
    # spaces are legal in ARM resource names (e.g. "My - Alert Rule"); only
    # block traversal and chars that could break the URL / inject a query.
    if not m or ".." in rid or any(c in rid for c in "?#\n\r\t"):
        return False
    subs = {s.lower() for s in poller.subs}
    return (not subs) or (m.group(1).lower() in subs)


# small TTL cache so re-opening a chart / query table is instant and we don't
# hammer (or get throttled by) Azure. Keyed on the request args.
_CACHE: dict = {}
_CACHE_TTL = 45.0


def _cache_get(key):
    v = _CACHE.get(key)
    return v[1] if v and (time.time() - v[0] < _CACHE_TTL) else None


def _cache_put(key, val):
    _CACHE[key] = (time.time(), val)
    if len(_CACHE) > 200:  # opportunistic prune of stale entries
        cut = time.time() - _CACHE_TTL
        for k in [k for k, (t, _) in _CACHE.items() if t < cut]:
            _CACHE.pop(k, None)


@app.get("/api/metrics/definitions")
async def api_metric_defs(resource: str):
    if not _valid_resource(resource):
        return JSONResponse({"error": "invalid resource"}, status_code=400)
    key = ("defs", resource)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        out = await asyncio.to_thread(poller.az.metric_definitions, resource)
        _cache_put(key, out)
        return out
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": str(ex)}, status_code=502)


@app.get("/api/metrics")
async def api_metrics(resource: str, names: str, hours: float = 6,
                      agg: str = "Average"):
    if not _valid_resource(resource):
        return JSONResponse({"error": "invalid resource"}, status_code=400)
    if not names:
        return JSONResponse([], status_code=200)
    key = ("metrics", resource, names, hours, agg)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        out = await asyncio.to_thread(poller.az.metrics, resource, names, hours, agg)
        _cache_put(key, out)
        return out
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": str(ex)}, status_code=502)


@app.get("/api/logquery")
async def api_logquery(rule: str):
    """Run a log-query alert rule's stored KQL and return its result table."""
    if not _valid_resource(rule) or "scheduledqueryrules" not in rule.lower():
        return JSONResponse({"error": "not a log-query rule"}, status_code=400)
    key = ("logq", rule)
    cached = _cache_get(key)
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await asyncio.to_thread(poller.az.log_alert_results, rule)
        if not res.get("error"):
            _cache_put(key, res)
        return JSONResponse(res, status_code=400 if res.get("error") else 200)
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": str(ex)}, status_code=502)


@app.get("/api/health")
async def api_health():
    """Poll + auth health, for the UI banner."""
    return {
        "auth_ok": poller.auth_ok,
        "poll_ok": poller.last_poll_ok,
        "last_poll_at": poller.last_poll_at,
        "error": poller.last_error,
        "quiet": poller.in_quiet_hours(),
    }


@app.get("/api/logs")
async def api_logs(n: int = 250):
    """Recent backend log lines for the in-app log viewer."""
    return {"lines": list(_LOGBUF)[-max(1, min(n, 800)):]}


@app.get("/api/config")
async def api_config_get():
    """The raw config.yaml text (edit it in the UI instead of an editor)."""
    try:
        with open(os.path.expanduser(CFG_PATH)) as f:
            return {"text": f.read(), "path": CFG_PATH}
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": str(ex)}, status_code=500)


@app.post("/api/config")
async def api_config_save(request: Request):
    """Validate + save config.yaml, then HOT-APPLY it without dropping the poll
    loop: mutate the live cfg dict and rebuild senders. (Plugins/host/port still
    need a restart.)"""
    body = await request.json()
    text = body.get("text", "")
    try:
        newcfg = yaml.safe_load(text) or {}
        validate_cfg(newcfg)
    except (yaml.YAMLError, ConfigError) as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    try:
        with open(os.path.expanduser(CFG_PATH), "w") as f:
            f.write(text)
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": f"write failed: {ex}"}, status_code=500)
    cfg.clear()
    cfg.update(newcfg)                 # same dict the poll loop reads → live
    poller.senders = build_senders(cfg)   # notify-channel changes apply now
    _CACHE.clear()
    log.info("config reloaded from UI (%d senders active)", len(poller.senders))
    return {"ok": True, "senders": len(poller.senders)}


@app.post("/api/reauth")
async def api_reauth():
    """Run `az login` (opens a browser on this machine) and refresh tokens."""
    def _login():
        try:
            r = subprocess.run(["az", "login"], capture_output=True, text=True,
                               timeout=300, shell=(os.name == "nt"))
            return r.returncode == 0
        except Exception as ex:  # noqa: BLE001
            log.error("az login failed: %s", ex)
            return False
    ok = await asyncio.to_thread(_login)
    poller.az._toks.clear()                 # force fresh ARM/LogAnalytics tokens
    _account_cache["data"] = None           # refresh the topbar account line
    if ok:
        poller.auth_ok = True
        poller.last_error = ""
    return {"ok": ok}


_PIM: dict = {"data": [], "ts": 0.0}


@app.get("/api/pim")
async def api_pim():
    """Your currently-activated PIM roles (cached ~3 min)."""
    if time.time() - _PIM["ts"] > 180:
        _PIM["data"] = await asyncio.to_thread(poller.az.pim_active, poller.subs)
        _PIM["ts"] = time.time()
    return {"active": _PIM["data"], "subs": sorted({p["sub"] for p in _PIM["data"]})}


@app.get("/api/plugins/list")
async def api_plugins_list():
    """Every registered plugin + whether it's enabled/loaded (for the UI)."""
    active = {p.name for p in poller.plugins}
    pconf = cfg.get("plugins") or {}
    return {"plugins": [
        {"name": n, "description": getattr(c, "description", ""),
         "enabled": bool((pconf.get(n) or {}).get("enabled")), "loaded": n in active}
        for n, c in sorted(plugbase._REGISTRY.items())]}


@app.post("/api/plugins/toggle")
async def api_plugins_toggle(name: str, enabled: bool):
    """Enable/disable a plugin's reactive hooks live (routes need a restart)."""
    cfg.setdefault("plugins", {}).setdefault(name, {})["enabled"] = enabled
    _save_plugin_override(name, enabled)
    poller.plugins = build_plugins(
        cfg, PluginContext(cfg=cfg, store=store, state=state))
    return {"ok": True, "loaded": [p.name for p in poller.plugins]}


@app.post("/api/plugins/upload")
async def api_plugins_upload(request: Request):
    """Save an uploaded .py into plugins/ and register it (disabled until
    toggled on). Local, auth-gated — it's your own code."""
    import re
    b = await request.json()
    fn = os.path.basename(b.get("filename", ""))
    text = b.get("text", "")
    if not re.match(r"^[A-Za-z0-9_]+\.py$", fn) or fn in ("base.py", "__init__.py"):
        return JSONResponse({"error": "filename must be a simple <name>.py"},
                            status_code=400)
    try:
        with open(os.path.join(os.path.dirname(plugbase.__file__), fn),
                  "w", encoding="utf-8") as f:
            f.write(text)
        plugin_discover()                  # import it → @register runs
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"error": str(ex)}, status_code=400)
    log.info("plugin file uploaded: %s", fn)
    return {"ok": True, "module": fn}


@app.get("/api/stream")
async def api_stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    # prime with current summary so a fresh tab paints immediately
    await q.put(("summary", store.summary()))

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


_AUTH_TOKEN = str(cfg.get("web", {}).get("auth_token") or "")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    # Optional shared-secret gate. Off (empty token) => open, which is safe on
    # the 127.0.0.1 default. When set (for LAN/0.0.0.0 use) every request must
    # carry the token via ?token=, X-Auth-Token header, or the cookie we drop
    # on first ?token= visit — so a browser/phone only needs the link once.
    if not _AUTH_TOKEN:
        return await call_next(request)
    supplied = (request.query_params.get("token")
                or request.headers.get("X-Auth-Token")
                or request.cookies.get("azmon_token") or "")
    if not hmac.compare_digest(supplied, _AUTH_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    resp = await call_next(request)
    if hmac.compare_digest(request.query_params.get("token") or "", _AUTH_TOKEN):
        resp.set_cookie("azmon_token", _AUTH_TOKEN, httponly=True,
                        samesite="lax", max_age=30 * 24 * 3600)
    return resp


_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    path = request.url.path
    # rate-limit the Azure-proxy endpoints (per client IP) to avoid throttling
    if path.startswith("/api/metrics") or path.startswith("/api/logquery"):
        ip = request.client.host if request.client else "?"
        if _ratelimited(f"{ip}:{path[:14]}"):
            return JSONResponse({"error": "rate limited, slow down"}, status_code=429)
    resp = await call_next(request)
    # Static assets change on every app update — revalidate so nothing is stale.
    if path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Content-Security-Policy"] = _CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    web = cfg.get("web", {})
    uvicorn.run(app, host=web.get("host", "127.0.0.1"),
                port=web.get("port", 8000), log_level="warning")


if __name__ == "__main__":
    main()
