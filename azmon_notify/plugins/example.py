"""A copy-me template plugin. Disabled by default (see `plugins:` in config.yaml).

It demonstrates both hooks:
  • on_new_alerts  — logs every newly-fired alert (do webhooks/DB writes/etc here)
  • register_routes — exposes GET /api/ext/example/stats

To build your own: copy this file, rename the class, change @register("name"),
add a matching block under `plugins:` in config.yaml, flip enabled: true.
"""
from __future__ import annotations

import logging

from .base import Plugin, register

log = logging.getLogger("azmon.plugin.example")


@register("example")
class ExamplePlugin(Plugin):
    description = "Template plugin — logs new alerts, serves /api/ext/example/stats"

    def setup(self) -> None:
        self._seen = 0
        self.greeting = self.conf.get("greeting", "example plugin ready")
        log.info("%s", self.greeting)

    def on_new_alerts(self, alerts: list) -> None:
        self._seen += len(alerts)
        for a in alerts:
            log.info("[example] new alert: %s / %s",
                     a.target_resource_name, a.rule)

    def register_routes(self, app) -> None:
        @app.get("/api/ext/example/stats")
        async def _example_stats():
            # plugins read live state through self.ctx
            summary = self.ctx.store.summary()
            return {
                "plugin": self.name,
                "greeting": self.greeting,
                "new_alerts_seen_since_boot": self._seen,
                "active_now": summary["total_active"],
            }
