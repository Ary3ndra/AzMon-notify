"""Core-logic tests — pure, no Azure/network. Run with: pytest"""
import datetime
import os
import tempfile

import pytest

from azmon_notify.azure_client import Alert, AzureClient
from azmon_notify.config import ConfigError, validate_cfg
from azmon_notify.poller import _in_quiet_hours, _mute_key, _sev_ok
from azmon_notify.state import State
from azmon_notify.store import AlertStore


def _db():
    return State(os.path.join(tempfile.mkdtemp(), "t.db"))


def mk(id, rule="R", ms="Log Analytics", sev="Sev2", sl="Warning",
       cond="Fired", hrs_ago=0, vm="vm1"):
    fired = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=hrs_ago)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return Alert(
        id=id, rule=f"/subscriptions/s/providers/microsoft.insights/scheduledqueryrules/{rule}",
        severity=sev, sev_label=sl, state="New", condition=cond, signal_type="Log",
        target_resource=f"/subscriptions/s/resourceGroups/rg/providers/microsoft.compute/virtualmachines/{vm}",
        target_resource_name=vm, target_resource_group="rg", fired_at=fired,
        description="", subscription="s", monitor_service=ms)


# ── azure_client._parse ─────────────────────────────────────────────────
def test_parse_uses_instance_guid_not_name():
    item = {"name": "MyRule",
            "id": "/subscriptions/x/providers/Microsoft.AlertsManagement/alerts/GUID-123",
            "properties": {"essentials": {"severity": "Sev1", "alertRule": "MyRule",
                                          "monitorCondition": "Fired"}}}
    a = AzureClient._parse(item, "sub1")
    assert a.id == "GUID-123"          # trailing GUID, not the reused rule name


def test_parse_null_fields_get_defaults():
    item = {"name": "n", "id": "/subscriptions/x/providers/Microsoft.AlertsManagement/alerts/g",
            "properties": {"essentials": {"severity": None, "targetResourceName": None,
                                          "description": None}}}
    a = AzureClient._parse(item, "s")
    assert a.severity == "Sev4" and a.target_resource_name == "?" and a.description == ""


def test_alert_derived_views():
    a = mk("g", rule="DEV_High_Memory_Alert")
    d = a.to_dict()
    assert d["rule_name"] == "DEV_High_Memory_Alert"
    assert d["alert_kind"] == "Log Query"
    assert "virtualmachines" in d["resource_type"]
    assert d["portal_url"].startswith("https://portal.azure.com")


# ── state ───────────────────────────────────────────────────────────────
def test_upsert_new_then_update():
    st = _db()
    assert st.upsert(mk("a"))["is_new"] is True
    assert st.upsert(mk("a"))["is_new"] is False
    assert st.seen_count() == 1


def test_prune_removes_old():
    st = _db()
    st.upsert(mk("old"))
    st._db.execute("UPDATE seen SET last_seen=? WHERE alert_id='old'",
                   (__import__("time").time() - 30 * 3600,))
    st.upsert(mk("fresh")); st.commit()
    assert st.prune(24) == 1 and st.seen_count() == 1


def test_purge_by_age_and_all():
    st = _db()
    st.upsert(mk("recent", hrs_ago=0)); st.upsert(mk("stale", hrs_ago=5))
    st.commit()
    assert st.purge(3) == 1                 # removes the 5h-old one
    assert st.purge(0) == 1 and st.seen_count() == 0


def test_mute_unmute_expiry():
    import time as _t
    st = _db()
    st.mute("K", 0)                          # forever
    assert "K" in st.muted_map()
    st.unmute("K")
    assert st.muted_map() == {}
    # an expired snooze is auto-cleaned on read
    st._db.execute("INSERT OR REPLACE INTO muted VALUES (?,?)", ("E", _t.time() - 10))
    st._db.commit()
    assert "E" not in st.muted_map()


def test_ack_and_counts():
    st = _db()
    st.upsert(mk("a")); st.upsert(mk("b", cond="Resolved")); st.commit()
    st.ack("a")
    c = st.counts()
    assert c["total"] == 2 and c["dismissed"] == 1


# ── store: smart grouping / sort / timeframe / mute sink ────────────────
def test_smart_grouping_collapses_repeats():
    st = _db()
    for i in range(5):
        st.upsert(mk(f"pss{i}", rule="PSS-Check", vm=f"vm{i}"))
    st.upsert(mk("sh", rule="X", ms="ServiceHealth"))
    st.upsert(mk("cpu", rule="High_CPU"))
    st.commit()
    groups = AlertStore(st).query(section="open", group_by="smart")
    keys = {g["key"]: g["count"] for g in groups}
    assert keys["PSS-Check"] == 5           # 5 VMs -> one group
    assert "Global Service Health" in keys


def test_timeframe_filter():
    st = _db()
    st.upsert(mk("a", hrs_ago=0.5)); st.upsert(mk("b", hrs_ago=5)); st.commit()
    store = AlertStore(st)
    assert len(store.query(section="open", group_by="none")) == 2
    assert len(store.query(section="open", group_by="none", within_hours=1)) == 1


def test_muted_group_sinks_last_and_flagged():
    st = _db()
    st.upsert(mk("a", rule="Noisy")); st.upsert(mk("b", rule="Important", sev="Sev0", sl="Critical"))
    st.commit()
    st.mute("Noisy", 0)
    groups = AlertStore(st).query(section="open", group_by="smart")
    assert groups[-1]["key"] == "Noisy" and groups[-1]["muted"] is True


# ── poller helpers ──────────────────────────────────────────────────────
def test_sev_ok():
    assert _sev_ok(mk("a", sev="Sev0"), 3) is True
    assert _sev_ok(mk("a", sev="Sev4"), 3) is False


def test_mute_key_matches_smart():
    assert _mute_key(mk("a", rule="High_CPU")) == "High_CPU"
    assert _mute_key(mk("a", ms="ServiceHealth")) == "Global Service Health"


def test_quiet_hours_wrap_midnight():
    assert _in_quiet_hours({"quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"}}) is True
    assert _in_quiet_hours({"quiet_hours": {"enabled": False}}) is False


# ── config validation ───────────────────────────────────────────────────
def _good():
    return {"lookback": "1d", "min_severity": 4, "poll_interval_seconds": 30,
            "web": {"port": 8000}}


def test_validate_ok():
    assert validate_cfg(_good()) is not None


@pytest.mark.parametrize("mut", [
    {"lookback": "5m"}, {"min_severity": 9}, {"poll_interval_seconds": 0},
    {"web": {"port": 0}}, {"quiet_hours": {"enabled": True, "start": "25:00", "end": "07:00"}},
])
def test_validate_rejects_bad(mut):
    cfg = _good(); cfg.update(mut)
    with pytest.raises(ConfigError):
        validate_cfg(cfg)
