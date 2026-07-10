"""Pluggable notification channels. New channel = subclass Notifier,
@register it, enable in config. (Your 'room for features'.)
"""
from __future__ import annotations

import logging

log = logging.getLogger("azmon.send")
_REGISTRY: dict[str, type["Notifier"]] = {}


def register(name: str):
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


def build_senders(cfg: dict) -> list["Notifier"]:
    out: list[Notifier] = []
    for name, sconf in (cfg.get("senders") or {}).items():
        if not sconf or not sconf.get("enabled"):
            continue
        cls = _REGISTRY.get(name)
        if not cls:
            log.warning("unknown sender '%s' — skipping", name)
            continue
        try:
            out.append(cls(sconf))
            log.info("sender enabled: %s", name)
        except Exception as ex:  # noqa: BLE001
            log.error("sender '%s' init failed: %s", name, ex)
    return out


class Notifier:
    def __init__(self, conf: dict):
        self.conf = conf

    def send_group(self, group_key: str, alerts: list) -> None:
        raise NotImplementedError

    @staticmethod
    def title(group_key: str, alerts: list) -> str:
        worst = min(a.severity for a in alerts)
        from ..constants import SEV_LABEL
        return f"[{SEV_LABEL.get(worst, worst)}] {group_key} - {len(alerts)} alert(s)"

    @staticmethod
    def body(alerts: list) -> str:
        return "\n".join(f"• {a.sev_label}: {a.rule}  ({a.fired_at})"
                         for a in alerts)

    @staticmethod
    def worst_severity(alerts: list) -> int:
        return int(min(a.severity for a in alerts)[3:])  # "Sev0"->0
