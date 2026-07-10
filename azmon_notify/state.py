"""Sqlite store: a rolling window of every alert instance seen recently
(fired or resolved), plus which ones the user has acknowledged in the UI.

An alert that resolves is updated in place (monitor_condition -> "Resolved",
resolved_at set) rather than removed, so it shows up in the Closed section —
but once we haven't seen it in over `retention_hours` (default 24h, see
Poller.prune), it's deleted for good. That keeps the db from growing forever
while never touching anything Azure is still actively reporting (still-firing
alerts get last_seen refreshed every poll, so they're never eligible).
Keyed on the alert GUID so restarts and overlapping poll windows never
duplicate anything.
"""
from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("azmon.state")

# Defensive caps so one huge query never hands the API/UI an unbounded
# result set — belt-and-suspenders on top of the 24h prune below.
_OPEN_ROWS_LIMIT = 5000
_CLOSED_ROWS_LIMIT = 2000

_COLUMNS = {
    "subscription": "TEXT", "rule": "TEXT", "severity": "TEXT",
    "sev_label": "TEXT", "monitor_condition": "TEXT", "azure_state": "TEXT",
    "signal_type": "TEXT", "monitor_service": "TEXT", "target_resource": "TEXT",
    "target_resource_name": "TEXT", "target_resource_group": "TEXT",
    "fired_at": "TEXT", "description": "TEXT",
    "first_seen": "REAL", "last_seen": "REAL", "resolved_at": "REAL",
}


class State:
    """One sqlite connection, shared by the background poll thread and the
    FastAPI event-loop thread (route handlers read it directly, synchronously).
    Python's sqlite3 module does not serialize access across threads on its
    own even with check_same_thread=False, so every method below takes
    `_lock` — that matters more, not less, as alert volume grows.
    """

    def __init__(self, path: str):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # WAL: readers (web) never block the writer (poll thread) and vice
            # versa; NORMAL sync is safe with WAL and much faster on spinning/OneDrive.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS seen (alert_id TEXT PRIMARY KEY)")
            self._migrate()
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS acked ("
                " alert_id TEXT PRIMARY KEY, acked_at REAL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS muted ("
                " mkey TEXT PRIMARY KEY, until REAL)")   # until=0 => forever
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_condition"
                " ON seen(monitor_condition)")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_fired ON seen(fired_at)")
            self._db.commit()

    def _migrate(self) -> None:
        """Add any columns missing from an older 'seen' table. Never drops data."""
        existing = {r["name"] for r in self._db.execute("PRAGMA table_info(seen)")}
        for col, sqltype in _COLUMNS.items():
            if col not in existing:
                self._db.execute(f"ALTER TABLE seen ADD COLUMN {col} {sqltype}")
        self._db.commit()

    # ── alert history (dedup + persistent record) ──────────────────────
    def is_new(self, alert_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM seen WHERE alert_id=?", (alert_id,)).fetchone() is None

    def upsert(self, alert) -> dict:
        """Insert or update the permanent record for one fetched alert.
        Returns {"is_new": bool, "just_resolved": bool}.
        """
        now = time.time()
        now_resolved = alert.condition == "Resolved"
        with self._lock:
            row = self._db.execute(
                "SELECT monitor_condition, first_seen, resolved_at FROM seen"
                " WHERE alert_id=?", (alert.id,)).fetchone()

            if row is None:
                self._db.execute(
                    "INSERT INTO seen (alert_id, subscription, rule, severity,"
                    " sev_label, monitor_condition, azure_state, signal_type,"
                    " monitor_service, target_resource, target_resource_name,"
                    " target_resource_group, fired_at, description,"
                    " first_seen, last_seen, resolved_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (alert.id, alert.subscription, alert.rule, alert.severity,
                     alert.sev_label, alert.condition, alert.state, alert.signal_type,
                     alert.monitor_service, alert.target_resource,
                     alert.target_resource_name, alert.target_resource_group,
                     alert.fired_at, alert.description,
                     now, now, now if now_resolved else None))
                return {"is_new": True, "just_resolved": now_resolved}

            was_resolved = row["monitor_condition"] == "Resolved"
            resolved_at = row["resolved_at"]
            if now_resolved and resolved_at is None:
                resolved_at = now
            elif not now_resolved:
                resolved_at = None
            self._db.execute(
                "UPDATE seen SET severity=?, sev_label=?, monitor_condition=?,"
                " azure_state=?, description=?, last_seen=?, resolved_at=?"
                " WHERE alert_id=?",
                (alert.severity, alert.sev_label, alert.condition, alert.state,
                 alert.description, now, resolved_at, alert.id))
            return {"is_new": False, "just_resolved": now_resolved and not was_resolved}

    def open_rows(self, limit: int = _OPEN_ROWS_LIMIT) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM seen WHERE monitor_condition IS NOT 'Resolved'"
                " ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()

    def closed_rows(self, limit: int = _CLOSED_ROWS_LIMIT) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM seen WHERE monitor_condition IS 'Resolved'"
                " ORDER BY COALESCE(resolved_at, last_seen) DESC LIMIT ?",
                (limit,)).fetchall()

    def prune(self, retention_hours: float) -> int:
        """Delete anything we haven't seen in over `retention_hours`. A still-
        firing alert gets last_seen refreshed every poll, so this only ever
        catches alerts Azure itself has stopped reporting (resolved-and-aged-
        out, or its lookback window rolled past it). Returns rows removed.
        """
        cutoff = time.time() - retention_hours * 3600
        with self._lock:
            cur = self._db.execute("DELETE FROM seen WHERE last_seen < ?", (cutoff,))
            removed = cur.rowcount
            self._db.execute(
                "DELETE FROM acked WHERE alert_id NOT IN (SELECT alert_id FROM seen)")
            self._db.commit()
            return removed

    def purge(self, older_than_hours: float) -> int:
        """User-triggered clear. older_than_hours=0 wipes the whole DB;
        otherwise deletes alerts that FIRED more than that long ago (by
        fired_at). Returns rows removed. Orphaned acks are cleaned up too."""
        with self._lock:
            if older_than_hours <= 0:
                n = self._db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
                self._db.execute("DELETE FROM seen")
                self._db.execute("DELETE FROM acked")
                self._db.commit()
                return n
            cutoff_iso = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=older_than_hours)
            ).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
            cur = self._db.execute("DELETE FROM seen WHERE fired_at < ?", (cutoff_iso,))
            removed = cur.rowcount
            self._db.execute(
                "DELETE FROM acked WHERE alert_id NOT IN (SELECT alert_id FROM seen)")
            self._db.commit()
            return removed

    def counts(self) -> dict:
        """Row stats for the settings/status panel."""
        with self._lock:
            seen = self._db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            opn = self._db.execute(
                "SELECT COUNT(*) FROM seen WHERE monitor_condition IS NOT 'Resolved'"
            ).fetchone()[0]
            ack = self._db.execute("SELECT COUNT(*) FROM acked").fetchone()[0]
            return {"total": seen, "open": opn, "closed": seen - opn, "dismissed": ack}

    # ── ack (UI dismiss) ────────────────────────────────────────────────
    def ack(self, alert_id: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO acked VALUES (?,?)",
                             (alert_id, time.time()))
            self._db.commit()

    def unack(self, alert_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM acked WHERE alert_id=?", (alert_id,))
            self._db.commit()

    def unack_all(self) -> int:
        """Clear every dismissal (restore all alerts to the Active view)."""
        with self._lock:
            cur = self._db.execute("DELETE FROM acked")
            self._db.commit()
            return cur.rowcount

    def acked_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM acked").fetchone()[0]

    def is_acked(self, alert_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM acked WHERE alert_id=?",
                (alert_id,)).fetchone() is not None

    def acked_ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT alert_id FROM acked")}

    # ── mute / snooze (per rule / smart-group key) ──────────────────────
    def mute(self, mkey: str, hours: float = 0) -> None:
        """Mute a rule/group key. hours=0 => forever, else snooze that long."""
        until = 0.0 if hours <= 0 else time.time() + hours * 3600
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO muted VALUES (?,?)", (mkey, until))
            self._db.commit()

    def unmute(self, mkey: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM muted WHERE mkey=?", (mkey,))
            self._db.commit()

    def muted_map(self) -> dict[str, float]:
        """Active mutes only — expired snoozes are cleaned up on read."""
        now = time.time()
        with self._lock:
            rows = self._db.execute("SELECT mkey, until FROM muted").fetchall()
            expired = [r["mkey"] for r in rows if r["until"] and r["until"] < now]
            for k in expired:
                self._db.execute("DELETE FROM muted WHERE mkey=?", (k,))
            if expired:
                self._db.commit()
            return {r["mkey"]: r["until"] for r in rows
                    if not (r["until"] and r["until"] < now)}

    def commit(self) -> None:
        with self._lock:
            self._db.commit()

    def seen_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
