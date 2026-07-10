"""Headless mode: poll + native/push notifications only, no web UI.
Use the web console (azmon_notify.web.app) for the dashboard.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import ConfigError, load_cfg, validate_cfg
from .poller import Poller
from .state import State
from .store import AlertStore

log = logging.getLogger("azmon")


def main():
    ap = argparse.ArgumentParser(description="azmon-notify headless poller")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    args = ap.parse_args()
    try:
        cfg = validate_cfg(load_cfg(args.config))
    except ConfigError as ex:
        print(f"[!] {ex}", file=sys.stderr)
        raise SystemExit(2)
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "INFO")),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    state = State(cfg["state_db"])
    poller = Poller(cfg, AlertStore(state), state)
    try:
        while True:
            try:
                poller.poll_once()
            except Exception as ex:  # noqa: BLE001
                log.error("poll cycle failed: %s", ex)
            if args.once:
                break
            time.sleep(cfg["poll_interval_seconds"])
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
