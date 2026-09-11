import json
from datetime import datetime, timedelta, timezone

import pytest

from subs_pool.modules.codex.accounts import AccountError, AccountStore
from subs_pool.modules.codex.quota_store import QuotaStore, iso
from fakes import write_auth_fixture


def test_import_list_and_status_never_exposes_token(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    store = AccountStore()

    store.import_account("personal", str(auth), weight=2)
    accounts = store.list()
    assert [a.ref for a in accounts] == ["personal"]

    status = accounts[0].to_status_dict()
    assert status["ref"] == "personal"
    assert status["weight"] == 2
    assert status["auth_present"] is True
    assert "quota" not in status
    # No token/secret field anywhere in the status view.
    dumped = json.dumps(status)
    assert "access_token" not in dumped
    assert "refresh_token" not in dumped
    assert "rt-1" not in dumped


def test_enable_disable_and_weight(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("work", str(auth))

    store.set_enabled("work", False)
    assert store.get("work").enabled is False
    assert store.eligible() == []

    store.set_enabled("work", True)
    store.set_weight("work", 5)
    assert store.get("work").weight == 5
    assert store.eligible() == []


def test_unknown_ref_raises():
    store = AccountStore()
    with pytest.raises(AccountError):
        store.get("nope")
    with pytest.raises(AccountError):
        store.set_enabled("nope", True)


def test_weight_must_be_positive(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("a", str(auth))
    with pytest.raises(AccountError):
        store.set_weight("a", 0)


def test_set_auth_path_preserves_enabled_and_weight(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("work", str(auth), weight=5)
    store.set_enabled("work", False)

    new_auth = tmp_path / "new-auth.json"
    write_auth_fixture(new_auth)
    updated = store.set_auth_path("work", str(new_auth))

    assert updated.auth_path == str(new_auth)
    assert updated.weight == 5
    assert updated.enabled is False


def test_set_auth_path_unknown_ref_raises():
    store = AccountStore()
    with pytest.raises(AccountError):
        store.set_auth_path("nope", "/tmp/x.json")


def test_persists_across_new_store_instances(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    AccountStore().import_account("persist", str(auth), weight=3)
    reopened = AccountStore()
    assert reopened.get("persist").weight == 3


def test_legacy_quota_exhaustion_is_ignored_and_sidecar_controls_eligibility(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("quota", str(auth))
    account = store.get("quota")
    now = datetime.now(timezone.utc)
    sidecar = QuotaStore(store.root)
    sidecar.claim([account], attempt_id="attempt", owner_id="test", deadline_at=now + timedelta(seconds=20))
    sidecar.commit_success(
        account,
        attempt_id="attempt",
        sample={
            "source_at": iso(now),
            "checked_at": iso(now),
            "fresh_until": iso(now + timedelta(seconds=60)),
            "allowed": True,
            "limit_reached": False,
            "primary": {"used_percent": 10, "remaining_percent": 90, "reset_at": None, "window_seconds": None},
            "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
        },
    )
    assert [a.ref for a in AccountStore().eligible()] == ["quota"]

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["accounts"]["quota"]["quota_exhausted"] = True
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    assert [a.ref for a in AccountStore().eligible()] == ["quota"]
    assert "quota_exhausted" not in AccountStore().get("quota").to_status_dict()
