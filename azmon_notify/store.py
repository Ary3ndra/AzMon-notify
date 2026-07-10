"""Read-side query layer over the rolling alert history in `State`.

The poller writes straight into sqlite (state.upsert / state.prune); there's
no separate in-memory copy to keep in sync. This module just filters/sorts/
groups that history for the API: "open" (still Fired) vs "closed" (Resolved,
both bounded to the last `lookback`/retention window) sections, severity/text
filters, and a handful of sort + group-by options.
"""
from __future__ import annotations

import datetime
import time

from .azure_client import Alert
from .constants import SEV_ORD
from .state import State


def _fired_ts(row) -> float:
    """Epoch seconds the alert fired. Prefers Azure's fired_at (ISO); falls
    back to when we first saw it. Used by the time-frame filter."""
    fa = row["fired_at"] or ""
    try:
        return datetime.datetime.fromisoformat(fa.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return row["first_seen"] or 0.0

#: bucket all Service Health notices under one low-priority group (they're
#: global/platform-wide, not per-resource, and tend to flood).
SERVICE_HEALTH_GROUP = "Global Service Health"


def _rule_name(row) -> str:
    """Short alert-rule name (tail of its ARM id), the human 'what fired'."""
    rule = row["rule"] or ""
    return (rule.rsplit("/", 1)[-1] if "/" in rule else rule) or "(unnamed alert)"


def _smart_key(row) -> str:
    """Collapse the noise: every Service Health notice into one bucket, and
    everything else by its rule name — so the same check firing across 50 VMs
    shows as one group instead of 50."""
    if (row["monitor_service"] or "").strip().lower() == "servicehealth":
        return SERVICE_HEALTH_GROUP
    return _rule_name(row)


_SORTS = {
    # (key function, reverse) — fired_at is an ISO-8601 string from Azure, so
    # lexicographic order already matches chronological order.
    "fired_desc": (lambda r: r["fired_at"] or "", True),
    "fired_asc": (lambda r: r["fired_at"] or "", False),
    "severity": (lambda r: (SEV_ORD.get(r["sev_label"], 5), r["fired_at"] or ""), False),
    "resource": (lambda r: (r["target_resource_name"] or "").lower(), False),
    "name": (lambda r: _rule_name(r).lower(), False),
}
DEFAULT_SORT = "fired_desc"


def _row_field(row, by: str) -> str:
    # Some alert types (e.g. resource-group-scoped or subscription-scoped
    # rules) genuinely have no target resource/rule name — fall back to a
    # placeholder rather than propagating None into sorting/grouping.
    if by == "smart":
        return _smart_key(row)
    value = {
        "targetResourceName": row["target_resource_name"],
        "targetResourceGroup": row["target_resource_group"],
        "severity": row["sev_label"],
        "alertRule": row["rule"],
        "signalType": row["signal_type"],
        "subscription": row["subscription"],
    }.get(by, row["target_resource_name"])
    return value or "(unassigned)"


def _matches_query(row, q: str) -> bool:
    if not q:
        return True
    hay = f"{row['target_resource_name']} {row['rule']} {row['target_resource_group']}".lower()
    return q in hay


def _row_to_dict(row, acked_ids: set[str], muted_keys: set[str] | None = None) -> dict:
    a = Alert(
        id=row["alert_id"], rule=row["rule"], severity=row["severity"],
        sev_label=row["sev_label"], state=row["azure_state"],
        condition=row["monitor_condition"], signal_type=row["signal_type"],
        target_resource=row["target_resource"],
        target_resource_name=row["target_resource_name"],
        target_resource_group=row["target_resource_group"],
        fired_at=row["fired_at"], description=row["description"] or "",
        subscription=row["subscription"],
        monitor_service=(row["monitor_service"] if "monitor_service" in row.keys() else ""))
    d = a.to_dict()
    d.update(acked=row["alert_id"] in acked_ids, first_seen=row["first_seen"],
              last_seen=row["last_seen"], resolved_at=row["resolved_at"],
              mute_key=_smart_key(row),
              muted=bool(muted_keys) and _smart_key(row) in muted_keys)
    return d


class AlertStore:
    def __init__(self, state: State):
        self.state = state
        self._last_updated = 0.0

    def touch(self) -> None:
        """Called once per poll cycle so the UI knows how fresh the data is."""
        self._last_updated = time.time()

    @property
    def last_updated(self) -> float:
        return self._last_updated

    def summary(self) -> dict:
        acked = self.state.acked_ids()
        counts = {"Critical": 0, "Error": 0, "Warning": 0,
                  "Informational": 0, "Verbose": 0}
        active = 0
        dismissed_open = 0
        worst = 5
        for row in self.state.open_rows():
            if row["alert_id"] in acked:
                dismissed_open += 1
                continue
            counts[row["sev_label"]] = counts.get(row["sev_label"], 0) + 1
            active += 1
            worst = min(worst, SEV_ORD.get(row["sev_label"], 5))
        return {
            "total_active": active,
            "counts": counts,
            "worst": worst,
            # how many still-firing alerts are hidden because they were
            # dismissed — lets the UI offer to bring them back.
            "dismissed_hidden": dismissed_open,
            "last_updated": self._last_updated,
        }

    def query(self, *, section: str = "open", severities: set[str] | None = None,
              text: str = "", show_acked: bool = False, sort: str = DEFAULT_SORT,
              group_by: str = "targetResourceName",
              within_hours: float | None = None) -> list[dict]:
        rows = self.state.closed_rows() if section == "closed" else self.state.open_rows()
        acked_ids = self.state.acked_ids()
        muted_keys = set(self.state.muted_map().keys())
        q = (text or "").strip().lower()
        cutoff = (time.time() - within_hours * 3600) if within_hours else None

        filtered = []
        for row in rows:
            if row["alert_id"] in acked_ids and not show_acked:
                continue
            if severities and row["sev_label"] not in severities:
                continue
            if cutoff is not None and _fired_ts(row) < cutoff:
                continue
            if not _matches_query(row, q):
                continue
            filtered.append(row)
        sort_key, reverse = _SORTS.get(sort, _SORTS[DEFAULT_SORT])
        filtered.sort(key=sort_key, reverse=reverse)

        if group_by == "none":
            return [_row_to_dict(r, acked_ids, muted_keys) for r in filtered]

        buckets: dict[str, list] = {}
        for row in filtered:
            buckets.setdefault(_row_field(row, group_by), []).append(row)

        # Order the GROUPS the same way as the chosen sort, not always by
        # severity — otherwise "Newest first" looks random across groups.
        # Each bucket's rows are already sorted, so bucket[0] is its leading
        # alert; rank groups by that same key.
        ordered = sorted(buckets.items(), key=lambda kv: sort_key(kv[1][0]),
                         reverse=reverse)
        # ...but keep the low-priority Global Service Health bucket pinned last
        # regardless of sort (stable sort preserves the order set above).
        ordered.sort(key=lambda kv: kv[0] == SERVICE_HEALTH_GROUP)
        # ...and sink fully-muted groups below everything else (also stable).
        def _grp_muted(rows):
            return all(_smart_key(r) in muted_keys for r in rows)
        ordered.sort(key=lambda kv: _grp_muted(kv[1]))

        out = []
        for key, bucket_rows in ordered:
            out.append({
                "key": key,
                "worst": min(SEV_ORD.get(r["sev_label"], 5) for r in bucket_rows),
                "count": len(bucket_rows),
                "muted": _grp_muted(bucket_rows),
                "alerts": [_row_to_dict(r, acked_ids, muted_keys) for r in bucket_rows],
            })
        return out
