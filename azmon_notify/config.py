"""Config loading + validation. Shared by the headless poller and the web app
so there's one place that parses config.yaml and fails with a clear message
instead of a stack trace deep in the poll loop.
"""
from __future__ import annotations

import os

import yaml

_VALID_LOOKBACK = {"1h", "1d", "7d", "30d"}


class ConfigError(ValueError):
    """Raised for an invalid config value, with a human-readable message."""


def load_cfg(path: str) -> dict:
    with open(os.path.expanduser(path)) as f:
        return yaml.safe_load(f) or {}


def validate_cfg(cfg: dict) -> dict:
    """Check the values we depend on; raise ConfigError with a clear message.
    Returns the config unchanged so callers can `cfg = validate_cfg(load_cfg(p))`.
    """
    def bad(msg: str):
        raise ConfigError(f"config.yaml: {msg}")

    if cfg.get("lookback") not in _VALID_LOOKBACK:
        bad(f"lookback must be one of {sorted(_VALID_LOOKBACK)}, got {cfg.get('lookback')!r}")

    sev = cfg.get("min_severity")
    if not isinstance(sev, int) or not 0 <= sev <= 4:
        bad(f"min_severity must be an int 0..4, got {sev!r}")

    pi = cfg.get("poll_interval_seconds")
    if not isinstance(pi, (int, float)) or pi <= 0:
        bad(f"poll_interval_seconds must be a positive number, got {pi!r}")

    qh = cfg.get("quiet_hours") or {}
    if qh.get("enabled"):
        for k in ("start", "end"):
            v = str(qh.get(k, ""))
            if not (len(v) == 5 and v[2] == ":" and v[:2].isdigit()
                    and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60):
                bad(f"quiet_hours.{k} must be HH:MM (00:00–23:59), got {v!r}")

    web = cfg.get("web") or {}
    port = web.get("port", 8000)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        bad(f"web.port must be 1..65535, got {port!r}")

    return cfg
