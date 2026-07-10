# Plugins / Extensions

The "room" for add-ons. A plugin loads with **zero edits to any core file** —
you write one module, register it, and enable it in config.

## Write one in 4 steps

1. **Copy the template.** Duplicate [`example.py`](example.py) → `my_plugin.py`
   in this folder.

2. **Subclass `Plugin` and register it.**
   ```python
   from .base import Plugin, register

   @register("my_plugin")            # the name you'll use in config.yaml
   class MyPlugin(Plugin):
       def setup(self):
           self.webhook = self.conf["webhook"]      # your config block

       def on_new_alerts(self, alerts):             # optional hook
           for a in alerts:
               ...  # POST to Slack, write a DB, enrich, route, whatever

       def register_routes(self, app):              # optional hook
           @app.get("/api/ext/my_plugin/ping")
           async def _ping():
               return {"ok": True, "active": self.ctx.store.summary()["total_active"]}
   ```

3. **Make it importable.** Add one line to [`__init__.py`](__init__.py):
   ```python
   from . import my_plugin  # noqa: F401
   ```

4. **Enable it in `config.yaml`** under `plugins:`:
   ```yaml
   plugins:
     my_plugin:
       enabled: true
       webhook: "https://..."      # anything here shows up as self.conf
   ```

Restart the app — you'll see `plugin enabled: my_plugin` in the log.

## The contract

| Hook | When | Runs in |
|------|------|---------|
| `setup(self)` | once, at load | startup thread |
| `on_new_alerts(self, alerts)` | each poll, only when there are genuinely-new fired alerts | poll thread |
| `register_routes(self, app)` | once, at web startup (skipped in headless mode) | main thread |

Every plugin gets `self.conf` (its own config block) and `self.ctx`, a
[`PluginContext`](base.py) exposing:

- `self.ctx.cfg` — the full parsed config
- `self.ctx.store` — the query layer (`.summary()`, `.query(section=..., group_by=...)`)
- `self.ctx.state` — the raw sqlite `State` if you need history directly

## Guarantees

- **Isolation** — an exception in any hook is caught and logged; it never takes
  down polling or the web server.
- **Both modes** — `on_new_alerts` fires in the web console *and* headless
  (`run_headless.ps1`). `register_routes` only runs when the web server is up.
- **Namespacing** — mount your endpoints under `/api/ext/<name>/...` so they
  never collide with core routes.

> Notification channels (toast/ntfy/telegram) live in a separate, older
> registry under [`../senders/`](../senders/) — same idea, but specialized for
> "push a message somewhere". Use a **plugin** for anything more general.
