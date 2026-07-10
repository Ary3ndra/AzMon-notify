"""Extension points — the 'room' for plugins.

Drop a module in this package, subclass `Plugin`, decorate it with
`@register("your_name")`, then enable it under `plugins:` in config.yaml. It
loads with zero edits to any core file. This mirrors the senders registry
(azmon_notify/senders/base.py) but is general-purpose.

A plugin can do either or both of:
  • react to newly-fired alerts   -> override on_new_alerts()
  • expose its own HTTP endpoints  -> override register_routes()

Both hooks are optional; a plugin overrides only what it needs. Everything is
wrapped so a misbehaving plugin logs and is skipped — it can never take the
poller or the web server down (same non-fatal philosophy as the rest of the app).

See example.py for a working, copy-me template.
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
from dataclasses import dataclass

log = logging.getLogger("azmon.plugin")

_REGISTRY: dict[str, type["Plugin"]] = {}


def discover() -> None:
    """Import every module in this package so its @register runs — lets users
    drop a new plugin file in and have it show up without editing __init__."""
    pkgdir = os.path.dirname(__file__)
    for m in pkgutil.iter_modules([pkgdir]):
        if m.name in ("base",):
            continue
        try:
            importlib.import_module(f"azmon_notify.plugins.{m.name}")
        except Exception as ex:  # noqa: BLE001
            log.error("plugin import '%s' failed: %s", m.name, ex)


def register(name: str):
    """Class decorator: make a Plugin subclass loadable under `name`."""
    def deco(cls):
        _REGISTRY[name] = cls
        cls.name = name
        return cls
    return deco


@dataclass
class PluginContext:
    """Handed to every plugin at construction — its window into the app.

    cfg   : the full parsed config dict
    store : AlertStore — query the current/closed alert view (store.summary(),
            store.query(...))
    state : State — the sqlite layer, if a plugin needs raw history access
    """
    cfg: dict
    store: object   # AlertStore (typed loosely to avoid an import cycle)
    state: object   # State


class Plugin:
    """Base class for all extensions. Override the hooks you care about."""

    #: unique registry name, set by @register
    name: str = "plugin"
    #: one-line human description, shown in the UI's extensions list (optional)
    description: str = ""

    def __init__(self, conf: dict, ctx: PluginContext):
        #: this plugin's own config sub-dict (its block under `plugins:`)
        self.conf = conf
        #: shared PluginContext (cfg / store / state)
        self.ctx = ctx
        self.setup()

    # ── lifecycle / hooks (all optional) ────────────────────────────────
    def setup(self) -> None:
        """One-time init: open connections, read `self.conf`, etc. Runs once
        when the plugin is loaded. Raise to abort loading this one plugin."""

    def on_new_alerts(self, alerts: list) -> None:
        """Called each poll with the list of genuinely-new fired alerts (never
        empty when called). Runs in the poll thread, so keep it quick or offload
        to a thread/queue. Fires in both web and headless modes."""

    def register_routes(self, app) -> None:
        """Called once at web startup. Mount FastAPI routes here — namespace
        them under /api/ext/<name>/... to avoid colliding with core endpoints.
        Not called in headless mode (there is no web server there)."""


def build_plugins(cfg: dict, ctx: PluginContext) -> list["Plugin"]:
    """Instantiate every plugin marked `enabled: true` under `plugins:`."""
    out: list[Plugin] = []
    for name, conf in (cfg.get("plugins") or {}).items():
        if not conf or not conf.get("enabled"):
            continue
        cls = _REGISTRY.get(name)
        if not cls:
            log.warning("unknown plugin '%s' — skipping", name)
            continue
        try:
            out.append(cls(conf, ctx))
            log.info("plugin enabled: %s", name)
        except Exception as ex:  # noqa: BLE001
            log.error("plugin '%s' init failed: %s", name, ex)
    return out
