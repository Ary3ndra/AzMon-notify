"""Bucket alerts — your 'group by resource' view."""
from __future__ import annotations
from collections import defaultdict


def group(alerts: list, by: str) -> dict[str, list]:
    """{group_key: [alerts...]}, worst-severity bucket first, worst alert first."""
    buckets: dict[str, list] = defaultdict(list)
    for a in alerts:
        buckets[a.field(by)].append(a)
    for k in buckets:
        buckets[k].sort(key=lambda a: a.severity)
    return dict(sorted(buckets.items(),
                       key=lambda kv: min(a.severity for a in kv[1])))
