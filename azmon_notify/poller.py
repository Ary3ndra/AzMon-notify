"""The polling engine. One `poll_once()` = fetch all subs, rebuild the live
store, fire native/push senders for genuinely-new alerts. Sync on purpose
(requests-based); the web layer runs it in a thread on a timer.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import time

from azure.core.exceptions import ClientAuthenticationError

from .azure_client import AzureClient
from .grouping import group
from .plugins import build_plugins, PluginContext
from .state import State
from .store import AlertStore
from .senders import build_senders

log = logging.getLogger("azmon.poll")

# Local DB retention follows the same window we query Azure with, so the two
# never drift out of sync: querying 1d of history but keeping 7d locally (or
# vice versa) would be confusing. Falls back to 24h for an unrecognized value.
_LOOKBACK_HOURS = {"1h": 1, "1d": 24, "7d": 24 * 7, "30d": 24 * 30}


def _sev_ok(alert, min_sev: int) -> bool:
    return int(alert.severity[3:]) <= min_sev  # Sev0..4, lower = worse


def _mute_key(alert) -> str:
    """Same key store._smart_key uses, so muting a group == muting its rule."""
    if (alert.monitor_service or "").strip().lower() == "servicehealth":
        return "Global Service Health"
    return alert.rule_name()


def _in_quiet_hours(cfg: dict) -> bool:
    """True inside the configured quiet window (HH:MM, may wrap midnight)."""
    qh = cfg.get("quiet_hours") or {}
    if not qh.get("enabled"):
        return False
    now = time.strftime("%H:%M")
    start, end = str(qh.get("start", "22:00")), str(qh.get("end", "07:00"))
    return (start <= now < end) if start <= end else (now >= start or now < end)


class Poller:
    def __init__(self, cfg: dict, store: AlertStore, state: State):
        self.cfg = cfg
        self.store = store
        self.az = AzureClient(cfg["api_version"])
        self.state = state
        self.senders = build_senders(cfg)
        # extensions: built once, shared with the web layer (register_routes)
        self.plugins = build_plugins(cfg, PluginContext(cfg=cfg, store=store,
                                                        state=state))
        self.subs = cfg.get("subscriptions") or []
        self.first_run = True
        # health (for /api/health + the UI auth banner)
        self.last_poll_at: float = 0.0
        self.last_poll_ok: bool = False
        self.auth_ok: bool = True
        self.last_error: str = ""

    def ensure_subs(self) -> None:
        if not self.subs:
            log.info("discovering subscriptions...")
            self.subs = self.az.list_subscriptions()
            log.info("watching %d subscription(s)", len(self.subs))

    def _fetch_sub(self, sub: str) -> list:
        return self.az.fetch_alerts(
            sub,
            time_range=self.cfg["lookback"],
            monitor_condition=self.cfg["monitor_condition"],
            alert_state=self.cfg.get("alert_state", ""),
        )

    def poll_once(self) -> list:
        """Returns the list of NEW alerts (for SSE broadcast). Never raises."""
        self.last_poll_at = time.time()
        try:
            self.ensure_subs()
        except ClientAuthenticationError as ex:
            self.auth_ok = False; self.last_poll_ok = False
            self.last_error = "Azure sign-in expired — run az login"
            log.error("auth failed during discovery: %s", ex)
            return []
        except Exception as ex:  # noqa: BLE001
            self.last_poll_ok = False; self.last_error = str(ex)
            log.error("subscription discovery failed: %s", ex)
            return []
        if not self.subs:
            self.last_poll_ok = True
            return []

        # fetch all subscriptions concurrently (was sequential — ~10-15s for 20)
        fetched_all: list = []
        errors = 0
        with cf.ThreadPoolExecutor(max_workers=min(6, len(self.subs))) as pool:
            futs = {pool.submit(self._fetch_sub, s): s for s in self.subs}
            for f in cf.as_completed(futs):
                try:
                    fetched_all.extend(f.result())
                    self.auth_ok = True
                except ClientAuthenticationError as ex:
                    self.auth_ok = False
                    self.last_error = "Azure sign-in expired — run az login"
                    log.error("auth failed for sub %s: %s", futs[f], ex)
                except Exception as ex:  # noqa: BLE001
                    errors += 1
                    log.error("fetch failed for sub %s: %s", futs[f], ex)

        new: list = []
        for a in fetched_all:
            try:
                if not _sev_ok(a, self.cfg["min_severity"]):
                    continue
                result = self.state.upsert(a)
                # only toast brand-new alerts that are actually still firing —
                # a resolved alert seen for the first time (e.g. app was down
                # while it fired and cleared) shouldn't page anyone.
                if result["is_new"] and a.condition != "Resolved":
                    new.append(a)
            except Exception as ex:  # noqa: BLE001
                # one malformed record must never take the rest down with it.
                log.error("failed to process alert %s: %s",
                         getattr(a, "id", "?"), ex)
        self.state.commit()
        self.last_poll_ok = True
        self.last_error = "" if self.auth_ok else self.last_error
        retention_hours = _LOOKBACK_HOURS.get(self.cfg["lookback"], 24)
        removed = self.state.prune(retention_hours)
        if removed:
            log.info("pruned %d alert(s) not seen in over %gh", removed, retention_hours)

        # drop muted rules from notifications AND from the returned "new" set
        # (so neither a toast nor the in-page beep fires for them).
        muted = self.state.muted_map()
        if muted and new:
            new = [a for a in new if _mute_key(a) not in muted]

        # native / push notifications for new alerts (grouped)
        if new:
            if self.first_run and not self.cfg.get("notify_on_first_run", False):
                log.info("first run: seeded %d alerts silently", len(new))
                new = []  # don't toast history on boot, but DO show on dashboard
            elif _in_quiet_hours(self.cfg):
                log.info("quiet hours: %d new alert(s), no toast", len(new))
            else:
                self._notify(new)
        self.first_run = False
        if new:
            self._dispatch_plugins(new)
        return new

    def in_quiet_hours(self) -> bool:
        return _in_quiet_hours(self.cfg)

    def _dispatch_plugins(self, new: list) -> None:
        for p in self.plugins:
            try:
                p.on_new_alerts(new)
            except Exception as ex:  # noqa: BLE001
                log.error("plugin '%s' on_new_alerts failed: %s",
                         getattr(p, "name", "?"), ex)

    def _notify(self, new: list) -> None:
        buckets = group(new, self.cfg["group_by"])
        log.info("%d new alert(s) in %d group(s)", len(new), len(buckets))
        for key, items in buckets.items():
            for s in self.senders:
                try:
                    s.send_group(key, items)
                except Exception as ex:  # noqa: BLE001
                    log.error("sender error: %s", ex)
