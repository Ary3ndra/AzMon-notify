"""Talks to Azure Resource Manager. Raw REST for the alerts list — full control.

Auth: DefaultAzureCredential. On your Windows laptop it reuses your `az login`
session (Azure CLI). No service principal, no app reg, no admin approval, no
cost. On a server/container it can fall back to an SP via
AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID env. Token auto-refreshes.
"""
from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, asdict

import requests
from azure.identity import DefaultAzureCredential

from .constants import SEV_LABEL

log = logging.getLogger("azmon.azure")

ARM = "https://management.azure.com"
SCOPE = "https://management.azure.com/.default"
LOGS = "https://api.loganalytics.io/v1"
LOGS_SCOPE = "https://api.loganalytics.io/.default"

# Azure's own alert categorization (essentials.monitorService) → friendly label.
# This is the most reliable "what kind of alert is this" signal — it's what
# distinguishes a Service Health notice from a metric threshold at a glance.
_MONITOR_SERVICE_LABEL = {
    "servicehealth": "Service Health",
    "resourcehealth": "Resource Health",
    "platform": "Metric",
    "log analytics": "Log Query",
    "log alerts v2": "Log Query",
    "application insights": "App Insights",
    "smartdetector": "Smart Detector",
    "activity log - administrative": "Activity Log",
    "activity log - policy": "Activity Log",
    "activity log - autoscale": "Activity Log",
    "activity log - security": "Activity Log",
    "custom": "Custom",
    "vm insights": "VM Insights",
}
# Fallback: the rule's ARM provider sub-type (.../providers/microsoft.insights/<type>/..)
_RULE_TYPE_LABEL = {
    "scheduledqueryrules": "Log Query",
    "metricalerts": "Metric",
    "activitylogalerts": "Activity Log",
    "smartdetectoralertrules": "Smart Detector",
    "alertrules": "Alert Rule",
}


def _last_segment(arm_id: str) -> str:
    """Human tail of an ARM id (e.g. the rule or resource name)."""
    return arm_id.rsplit("/", 1)[-1] if arm_id and "/" in arm_id else arm_id


def _arm_part(arm_id: str, key: str) -> str:
    """Pull the value following `key` in an ARM path, case-insensitively
    (e.g. _arm_part(id, 'resourcegroups') -> the resource group name)."""
    if not arm_id:
        return ""
    segs = arm_id.strip("/").split("/")
    low = [s.lower() for s in segs]
    try:
        i = low.index(key.lower())
        return segs[i + 1]
    except (ValueError, IndexError):
        return ""


def _rule_provider_type(rule_arm_id: str) -> str:
    """The provider sub-type from a rule ARM id (…/providers/ns/<type>/name)."""
    if "/providers/" not in (rule_arm_id or ""):
        return ""
    tail = rule_arm_id.split("/providers/")[-1].split("/")
    return tail[1].lower() if len(tail) >= 2 else ""


@dataclass
class Alert:
    """Flattened view of one fired alert instance."""
    id: str                  # stable GUID — dedup / ack key
    rule: str
    severity: str            # "Sev0".."Sev4"
    sev_label: str
    state: str
    condition: str
    signal_type: str
    target_resource: str
    target_resource_name: str
    target_resource_group: str
    fired_at: str
    description: str
    subscription: str
    monitor_service: str = ""   # Azure's alert category (Service Health/Metric/…)

    # ── derived, human-friendly views of the ARM ids ────────────────────
    def rule_name(self) -> str:
        """The alert rule's short name (e.g. 'DEV_High_Memory_Alert')."""
        return _last_segment(self.rule) or self.rule or "(unnamed alert)"

    def alert_kind(self) -> str:
        """Best-effort 'what kind of alert': Service Health / Metric / Log
        Query / Activity Log / …, from Azure's monitorService, then the rule
        type, then the raw signal type."""
        ms = (self.monitor_service or "").strip().lower()
        if ms:
            return _MONITOR_SERVICE_LABEL.get(ms, self.monitor_service)
        kind = _RULE_TYPE_LABEL.get(_rule_provider_type(self.rule), "")
        return kind or self.signal_type or "Alert"

    def resource_name(self) -> str:
        """Target resource (VM) name, falling back to the resource id tail."""
        if self.target_resource_name and self.target_resource_name != "?":
            return self.target_resource_name
        return _last_segment(self.target_resource) or ""

    def resource_group(self) -> str:
        """Target resource group, falling back to whichever ARM id carries it."""
        if self.target_resource_group and self.target_resource_group != "?":
            return self.target_resource_group
        return (_arm_part(self.target_resource, "resourcegroups")
                or _arm_part(self.rule, "resourcegroups") or "")

    def resource_type(self) -> str:
        """Azure provider type of the target, e.g. 'microsoft.compute/
        virtualmachines' — drives the per-row resource icon. Empty for
        target-less alerts (Service Health etc.); the UI icons those by kind."""
        if "/providers/" not in (self.target_resource or ""):
            return ""
        tail = self.target_resource.split("/providers/")[-1].split("/")
        return f"{tail[0]}/{tail[1]}".lower() if len(tail) >= 2 else ""

    def field(self, name: str) -> str:
        return {
            "targetResourceName": self.target_resource_name,
            "targetResourceGroup": self.target_resource_group,
            "severity": self.sev_label,
            "alertRule": self.rule,
            "signalType": self.signal_type,
            "subscription": self.subscription,
        }.get(name, self.target_resource_name)

    def portal_url(self) -> str | None:
        """Deep link into the Azure portal. Prefers the target resource; for
        alerts with no target (e.g. Service Health) falls back to the alert
        rule resource so the link still lands somewhere useful. Opens in
        whatever browser tab/profile is already signed in — no tenant hard-coded."""
        target = self.target_resource or (self.rule if self.rule.startswith("/subscriptions/") else "")
        if not target:
            return None
        return f"https://portal.azure.com/#@/resource{target}/overview"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["portal_url"] = self.portal_url()
        # pre-computed human views so every consumer (SSE + query API) is uniform
        d["rule_name"] = self.rule_name()
        d["alert_kind"] = self.alert_kind()
        d["resource_name"] = self.resource_name()
        d["resource_group"] = self.resource_group()
        d["resource_type"] = self.resource_type()
        return d


class AzureClient:
    def __init__(self, api_version: str):
        self._cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._api_version = api_version
        self._toks: dict[str, tuple[str, float]] = {}   # scope -> (token, expiry)
        self._s = requests.Session()
        #: subscription id -> display name, filled in by list_subscriptions()
        self.sub_names: dict[str, str] = {}

    def _token(self, scope: str = SCOPE) -> str:
        tok, exp = self._toks.get(scope, (None, 0.0))
        if tok and time.time() < exp - 300:
            return tok
        t = self._cred.get_token(scope)
        self._toks[scope] = (t.token, t.expires_on)
        log.debug("refreshed token for %s", scope)
        return t.token

    def _headers(self, scope: str = SCOPE) -> dict:
        return {"Authorization": f"Bearer {self._token(scope)}"}

    def _pim_for_sub(self, sub: str, sub_names: dict) -> list[dict]:
        """PIM roles *you've activated* (assignmentType == Activated) at this
        subscription scope, with expiry. Uses the current-user filter."""
        url = (f"{ARM}/subscriptions/{sub}/providers/Microsoft.Authorization/"
               "roleAssignmentScheduleInstances")
        try:
            r = self._s.get(url, headers=self._headers(),
                            params={"api-version": "2020-10-01",
                                    "$filter": "asTarget()"}, timeout=30)
            if r.status_code != 200:
                return []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for it in r.json().get("value", []):
            p = it.get("properties", {}) or {}
            atype = p.get("assignmentType") or p.get("memberType")
            if atype != "Activated":
                continue
            exp = p.get("expandedProperties", {}) or {}
            out.append({
                "sub": sub,
                "sub_name": sub_names.get(sub, sub),
                "role": (exp.get("roleDefinition", {}) or {}).get("displayName", "role"),
                "scope": (exp.get("scope", {}) or {}).get("displayName")
                         or p.get("scope", sub),
                "expiry": p.get("endDateTime"),
            })
        return out

    def pim_active(self, subs: list[str]) -> list[dict]:
        """All your currently-activated PIM roles across the given subs."""
        import concurrent.futures as _cf
        out: list[dict] = []
        if not subs:
            return out
        with _cf.ThreadPoolExecutor(max_workers=min(8, len(subs))) as pool:
            for res in pool.map(lambda s: self._pim_for_sub(s, self.sub_names), subs):
                out.extend(res)
        return out

    def list_subscriptions(self) -> list[str]:
        url = f"{ARM}/subscriptions?api-version=2020-01-01"
        out: list[str] = []
        while url:
            r = self._s.get(url, headers=self._headers(), timeout=30)
            r.raise_for_status()
            body = r.json()
            for s in body.get("value", []):
                sid = s["subscriptionId"]
                out.append(sid)
                self.sub_names[sid] = s.get("displayName") or sid
            url = body.get("nextLink")
        return out

    def fetch_alerts(self, sub: str, *, time_range: str, monitor_condition: str,
                     alert_state: str) -> list[Alert]:
        params = {
            "api-version": self._api_version,
            "timeRange": time_range,
        }
        if monitor_condition:  # "" = both Fired and Resolved
            params["monitorCondition"] = monitor_condition
        if alert_state:
            params["alertState"] = alert_state

        url = (f"{ARM}/subscriptions/{sub}"
               f"/providers/Microsoft.AlertsManagement/alerts")
        alerts: list[Alert] = []
        while url:
            r = self._s.get(url, headers=self._headers(), params=params, timeout=60)
            params = None  # nextLink carries query already
            if r.status_code == 403:
                log.warning("403 on sub %s (no Reader?) — skipping", sub)
                return []
            if r.status_code == 429:  # throttled — honour Retry-After once
                wait = min(int(r.headers.get("Retry-After", "3") or 3), 15)
                log.warning("429 on sub %s — backing off %ss", sub, wait)
                time.sleep(wait)
                r = self._s.get(url, headers=self._headers(), params=params, timeout=60)
            r.raise_for_status()
            body = r.json()
            for item in body.get("value", []):
                alerts.append(self._parse(item, sub))
            url = body.get("nextLink")
        return alerts

    # ── Azure Monitor metrics (same ARM token/scope) ────────────────────
    def metric_definitions(self, resource_id: str) -> list[dict]:
        """What metrics this resource exposes — exactly what the portal's
        Metrics explorer lists (so 'memory %' only appears if AMA is present)."""
        url = f"{ARM}{resource_id}/providers/microsoft.insights/metricDefinitions"
        r = self._s.get(url, headers=self._headers(),
                        params={"api-version": "2018-01-01"}, timeout=30)
        r.raise_for_status()
        out = []
        for m in r.json().get("value", []):
            nm = m.get("name", {})
            out.append({
                "name": nm.get("value"),
                "label": nm.get("localizedValue") or nm.get("value"),
                "unit": m.get("unit", ""),
                "primary": m.get("primaryAggregationType", "Average"),
            })
        return out

    def metrics(self, resource_id: str, names: str, hours: float,
                agg: str = "Average") -> list[dict]:
        """Time-series for the given metric names over the last `hours`."""
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(hours=hours)
        interval = {1: "PT1M", 3: "PT5M", 6: "PT5M",
                    12: "PT15M", 24: "PT30M"}.get(int(hours), "PT15M")
        params = {
            "api-version": "2018-01-01",
            "metricnames": names,
            "timespan": f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
                        f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "interval": interval,
            "aggregation": agg,
        }
        url = f"{ARM}{resource_id}/providers/microsoft.insights/metrics"
        r = self._s.get(url, headers=self._headers(), params=params, timeout=60)
        r.raise_for_status()
        aggkey = agg.lower()
        out = []
        for m in r.json().get("value", []):
            ts = m.get("timeseries") or []
            pts = []
            if ts:
                for d in ts[0].get("data", []):
                    pts.append([d.get("timeStamp"), d.get(aggkey)])
            out.append({"name": m.get("name", {}).get("value"),
                        "unit": m.get("unit", ""), "points": pts})
        return out

    # ── log-query (KQL) alert results ───────────────────────────────────
    def get_query_rule(self, rule_id: str) -> dict:
        """Fetch a scheduledQueryRule's stored KQL, scope(s) and eval window."""
        r = self._s.get(f"{ARM}{rule_id}", headers=self._headers(),
                        params={"api-version": "2023-03-15-preview"}, timeout=30)
        r.raise_for_status()
        p = r.json().get("properties", {})
        crit = (p.get("criteria", {}).get("allOf") or [{}])[0]
        return {"query": crit.get("query", ""),
                "scopes": p.get("scopes", []),
                "window": p.get("windowSize", "PT1H")}

    def workspace_customer_id(self, ws_resource_id: str) -> str | None:
        r = self._s.get(f"{ARM}{ws_resource_id}", headers=self._headers(),
                        params={"api-version": "2022-10-01"}, timeout=30)
        r.raise_for_status()
        return r.json().get("properties", {}).get("customerId")

    def run_log_query(self, kql: str, timespan: str, *,
                      customer_id: str | None = None,
                      resource_id: str | None = None) -> dict:
        """Run KQL via the Log Analytics data API (separate token scope).
        Returns {columns, rows} of the primary result table."""
        headers = dict(self._headers(LOGS_SCOPE),
                       **{"Content-Type": "application/json"})
        body = {"query": kql, "timespan": timespan}
        if customer_id:
            url = f"{LOGS}/workspaces/{customer_id}/query"
        else:
            url = f"{LOGS}{resource_id}/query"        # resource-centric
        r = self._s.post(url, headers=headers, json=body, timeout=90)
        r.raise_for_status()
        tables = r.json().get("tables") or [{}]
        t = tables[0]
        return {"columns": [c["name"] for c in t.get("columns", [])],
                "rows": t.get("rows", [])}

    @staticmethod
    def _cap_query(kql: str, limit: int = 10000) -> str:
        """Append a row cap so a broad query can't pull half a million rows
        into the UI. Safe for any single tabular statement; skipped when the
        query renders a chart or has multiple statements (where it'd break)."""
        low = kql.lower()
        if ";" in kql or " render " in low or "\n| take " in low or "\n| limit " in low:
            return kql
        return kql.rstrip().rstrip("|").rstrip() + f"\n| take {limit}"

    def log_alert_results(self, rule_id: str) -> dict:
        """End-to-end: rule -> its KQL + scope -> run it -> table."""
        rule = self.get_query_rule(rule_id)
        if not rule["query"]:
            return {"error": "rule has no query"}
        scope = rule["scopes"][0] if rule["scopes"] else None
        kwargs = {}
        if scope and "/microsoft.operationalinsights/workspaces/" in scope.lower():
            cid = self.workspace_customer_id(scope)
            kwargs = {"customer_id": cid}
        elif scope:
            kwargs = {"resource_id": scope}
        else:
            return {"error": "rule has no scope"}
        res = self.run_log_query(self._cap_query(rule["query"]), rule["window"], **kwargs)
        res.update(query=rule["query"], window=rule["window"], scope=scope)
        return res

    @staticmethod
    def _parse(item: dict, sub: str) -> Alert:
        # Some alert types (subscription/resource-group-scoped rules, certain
        # Activity Log alerts) send these keys back as an explicit JSON null
        # rather than omitting them — `dict.get(key, default)` only applies
        # the default when the key is *missing*, so every field here uses
        # `or default` instead to also catch the null case.
        e = item.get("properties", {}).get("essentials", {}) or {}
        sev = e.get("severity") or "Sev4"
        # DEDUP KEY: the alert *instance* GUID is the trailing segment of the
        # ARM resource id (.../Microsoft.AlertsManagement/alerts/<guid>).
        # item["name"] is NOT unique here — for activity-log / service-health
        # alerts it's the rule's display name, so many distinct firings share
        # it and would collapse onto one row. The full id always ends in the
        # per-instance GUID; fall back to name only if id is somehow absent.
        arm_id = item.get("id") or ""
        instance_id = arm_id.rsplit("/", 1)[-1] if arm_id else (item.get("name") or "")
        return Alert(
            id=instance_id,
            rule=e.get("alertRule") or "?",
            severity=sev,
            sev_label=SEV_LABEL.get(sev, sev),
            state=e.get("alertState") or "?",
            condition=e.get("monitorCondition") or "?",
            signal_type=e.get("signalType") or "?",
            target_resource=e.get("targetResource") or "",
            target_resource_name=e.get("targetResourceName") or "?",
            target_resource_group=e.get("targetResourceGroup") or "?",
            fired_at=e.get("startDateTime") or e.get("lastModifiedDateTime") or "",
            description=e.get("description") or "",
            subscription=sub,
            monitor_service=e.get("monitorService") or "",
        )
