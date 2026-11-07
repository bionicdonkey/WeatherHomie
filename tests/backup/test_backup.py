"""Backup and restore of settings."""
import copy
import json
import os
import stat
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from stormtracker.backup import CHECK_TEXT, _derive, _seal
from stormtracker.bootstrap import Bootstrap
from stormtracker.main import create_app

PASS = "correct horse battery"
SOURCE = {"enabled": True, "config": {"station": "S9"}, "secrets": {"api_key": "k-original"}}
LOCS = [{"id": "home", "name": "Home", "lat": -33.87, "lon": 151.21}]
RULE = {"name": "Rain soon", "severity": "warning", "cooldown_min": 20, "notify_clear": False, "destinations": [],
        "params": {"type": "rain_eta", "min_peak_mmh": 2.0, "max_eta_min": 30}}


@asynccontextmanager
async def second_app(tmp_path, registry, master="z" * 40):
    """A different installation: its own database and its own MASTER_KEY."""
    d = tmp_path / "other"
    d.mkdir()
    boot = Bootstrap(database_url=f"sqlite+aiosqlite:///{d}/t.db", data_dir=d, master_key=master, cookie_secure=False, admin_password="another-long-password")
    app = create_app(boot, registry)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"username": "admin", "password": "another-long-password"})
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            yield app, c


async def configure(admin):
    assert (await admin.put("/api/locations", json=LOCS)).status_code == 200
    assert (await admin.put("/api/ui", json={"tile_url": "", "tile_attribution": "", "radar_opacity": 0.5, "geocoder_url": ""})).status_code == 200
    assert (await admin.put("/api/sources/fake", json=SOURCE)).status_code == 200
    assert (await admin.post("/api/alerts/rules", json=RULE)).status_code == 200
    r = await admin.put("/api/alerts/outputs/webhook", json={"enabled": True, "config": {}, "secrets": {"url": "http://hook.example/hook-secret"}})
    assert r.status_code == 200, r.text
    r = await admin.put("/api/auth/oidc/settings", json={"enabled": True, "issuer": "https://idp.example.com/app/", "client_id": "st", "public_url": "https://st.example.com",
                                                          "admin_groups": ["a"], "member_groups": ["m"], "client_secret": "oidc-secret"})
    assert r.status_code == 200, r.text


async def export(admin, secrets=True):
    r = await admin.post("/api/backup/export", json={"include_secrets": secrets, "passphrase": PASS if secrets else None})
    assert r.status_code == 200, r.text
    return r.json()


async def test_settings_only_backup_has_no_secrets(admin):
    await configure(admin)
    doc = await export(admin, secrets=False)
    assert doc["format"] == "stormtracker-backup" and doc["version"] == 1 and doc["secrets"] is None
    assert {"locations", "ui", "source.fake", "alerts.rules", "alert_output.webhook", "auth.oidc"} <= set(doc["settings"])
    text = json.dumps(doc)
    for s in ("k-original", "hook-secret", "oidc-secret"):
        assert s not in text
    assert not {"auth.epoch", "auth.revoked"} & set(doc["settings"])


async def test_backup_with_secrets_is_encrypted_and_needs_a_real_passphrase(admin):
    await configure(admin)
    doc = await export(admin)
    text = json.dumps(doc)
    for s in ("k-original", "hook-secret", "oidc-secret", PASS):
        assert s not in text
    assert set(doc["secrets"]["items"]) == {"source.fake.api_key", "alert_output.webhook.url", "auth.oidc.client_secret"}
    assert "meta.canary" not in doc["secrets"]["items"]
    assert doc["secrets"]["kdf"]["name"] == "scrypt"
    for body in ({"include_secrets": True}, {"include_secrets": True, "passphrase": "short"}):
        assert (await admin.post("/api/backup/export", json=body)).status_code == 422


async def test_restore_on_a_different_installation_with_another_master_key(admin, tmp_path, registry):
    await configure(admin)
    doc = await export(admin)
    async with second_app(tmp_path, registry) as (app2, c2):
        pv = (await c2.post("/api/backup/preview", json={"backup": doc, "passphrase": PASS})).json()
        assert {r["key"]: r["status"] for r in pv["settings"]}["locations"] == "new" and pv["secrets"]["count"] == 3
        assert any("Single sign-on" in w for w in pv["warnings"]) and any("webhook" in w for w in pv["warnings"])
        assert (await c2.get("/api/locations")).json() == []                                      # a preview changes nothing
        r = await c2.post("/api/backup/restore", json={"backup": doc, "passphrase": PASS})
        assert r.status_code == 200 and r.json()["restored"], r.text
        assert (await c2.get("/api/locations")).json() == LOCS
        assert (await c2.get("/api/ui")).json()["radar_opacity"] == 0.5
        assert [x["name"] for x in (await c2.get("/api/alerts/rules")).json()] == ["Rain soon"]
        src = [s for s in (await c2.get("/api/sources")).json() if s["meta"]["name"] == "fake"][0]["settings"]
        assert src["enabled"] and src["config"] == {"station": "S9"} and src["secrets_set"] == {"api_key": True}
        core2 = app2.state.core
        assert await core2.store.get_secret("source.fake.api_key") == "k-original"               # re-encrypted under this installation's key
        assert await core2.store.get_secret("auth.oidc.client_secret") == "oidc-secret"
        assert core2.scheduler.running("fake") is not None                                       # the restored source was started
        assert (await c2.get("/api/auth/oidc/settings")).json()["issuer"] == "https://idp.example.com/app/"
        assert "oidc-secret" not in (await c2.get("/api/auth/oidc/settings")).text


async def test_wrong_passphrase_and_no_passphrase(admin, tmp_path, registry):
    await configure(admin)
    doc = await export(admin)
    async with second_app(tmp_path, registry) as (app2, c2):
        r = await c2.post("/api/backup/restore", json={"backup": doc, "passphrase": "not the right one"})
        assert r.status_code == 422 and "wrong passphrase" in r.json()["detail"] and (await c2.get("/api/locations")).json() == []
        r = await c2.post("/api/backup/restore", json={"backup": doc})                           # settings only: secrets skipped, current ones kept
        assert r.status_code == 200 and any("no passphrase" in w for w in r.json()["warnings"]) and r.json()["secrets"]["included"] is False
        assert (await c2.get("/api/locations")).json() == LOCS
        assert await app2.state.core.store.get_secret("source.fake.api_key") is None


async def test_a_tampered_secret_or_setting_is_refused_and_nothing_changes(admin, tmp_path, registry):
    await configure(admin)
    doc = await export(admin)
    async with second_app(tmp_path, registry) as (app2, c2):
        bad = copy.deepcopy(doc)
        item = bad["secrets"]["items"]["source.fake.api_key"]
        bad["secrets"]["items"]["source.fake.api_key"] = item[:-6] + ("AAAAAA" if item[-6:] != "AAAAAA" else "BBBBBB")
        r = await c2.post("/api/backup/restore", json={"backup": bad, "passphrase": PASS})
        assert r.status_code == 422 and "tampered" in r.json()["detail"]
        # a secret moved to another slot must not decrypt (the name is bound into the encryption)
        swap = copy.deepcopy(doc)
        swap["secrets"]["items"]["source.fake.api_key"] = doc["secrets"]["items"]["alert_output.webhook.url"]
        assert (await c2.post("/api/backup/restore", json={"backup": swap, "passphrase": PASS})).status_code == 422
        # one bad setting among good ones: nothing is applied
        broken = copy.deepcopy(doc)
        broken["settings"]["ui"]["radar_opacity"] = 99
        r = await c2.post("/api/backup/restore", json={"backup": broken, "passphrase": PASS})
        assert r.status_code == 422 and "ui" in r.json()["detail"] and "nothing was changed" in r.json()["detail"]
        assert (await c2.get("/api/locations")).json() == []
        assert not (app2.state.core.boot.data_dir / "backups").exists()                          # and no snapshot was taken for a failed restore


async def test_unknown_keys_and_secret_names_are_ignored_not_written(admin, tmp_path, registry):
    await configure(admin)
    doc = await export(admin)
    async with second_app(tmp_path, registry) as (app2, c2):
        core2 = app2.state.core
        before_epoch = await core2.store.get("auth.epoch", 0)
        evil = copy.deepcopy(doc)
        evil["settings"]["auth.epoch"] = 999
        evil["settings"]["auth.revoked"] = {"x": 1}
        evil["settings"]["source.not_installed"] = {"enabled": True, "config": {}}
        evil["settings"]["totally.unknown"] = 1
        key = _derive(PASS, __import__("base64").b64decode(evil["secrets"]["kdf"]["salt"]), **{k: evil["secrets"]["kdf"][k] for k in ("n", "r", "p")})
        evil["secrets"]["items"]["meta.canary"] = _seal(key, "meta.canary", "pwned")
        evil["secrets"]["items"]["source.nope.token"] = _seal(key, "source.nope.token", "x")
        r = await c2.post("/api/backup/restore", json={"backup": evil, "passphrase": PASS})
        assert r.status_code == 200, r.text
        ignored = {i["key"] for i in r.json()["ignored"]}
        assert {"auth.epoch", "auth.revoked", "source.not_installed", "totally.unknown", "secret meta.canary", "secret source.nope.token"} <= ignored
        assert await core2.store.get("auth.epoch", 0) == before_epoch and await core2.store.get("source.not_installed") is None
        core2.store.get_secret  # canary is still the original: the app still starts and the key still checks out
        await core2.store.init()


async def test_secrets_cannot_hide_in_a_source_config(admin, tmp_path, registry):
    await configure(admin)
    doc = await export(admin, secrets=False)
    doc["settings"]["source.fake"]["config"]["api_key"] = "plain-text-secret"
    async with second_app(tmp_path, registry) as (app2, c2):
        r = await c2.post("/api/backup/restore", json={"backup": doc})
        assert r.status_code == 422 and "secret" in r.json()["detail"]
        assert await app2.state.core.store.get("source.fake") is None


async def test_files_that_are_not_valid_backups(admin):
    good = await export(admin, secrets=False)
    for bad in ({}, {"format": "other"}, {**good, "version": 2}, {**good, "version": "1"}, {**good, "settings": []}, {**good, "settings": {f"k{i}": 1 for i in range(600)}},
                {**good, "secrets": {"kdf": {"name": "scrypt", "salt": "AAAA", "n": 2 ** 30, "r": 8, "p": 1}, "check": "AAAA", "items": {}}},
                {**good, "secrets": {"kdf": {"name": "md5"}, "check": "x", "items": {}}}, {**good, "secrets": "nope"}):
        r = await admin.post("/api/backup/preview", json={"backup": bad, "passphrase": PASS})
        assert r.status_code == 422, bad
    assert "newer" in (await admin.post("/api/backup/preview", json={"backup": {**good, "version": 2}})).json()["detail"]
    assert (await admin.post("/api/backup/preview", json={"backup": "x"})).status_code == 422
    assert (await admin.post("/api/backup/preview", json={"backup": {**good, "pad": "x" * 2_100_000}})).status_code == 413


async def test_restore_replaces_what_is_there_and_undo_brings_it_back(admin, app):
    await configure(admin)
    first = await export(admin)
    # change things after the backup was made
    await admin.put("/api/locations", json=[{"id": "other", "name": "Elsewhere", "lat": -30.0, "lon": 151.0}])
    await admin.put("/api/sources/fake", json={"secrets": {"api_key": "k-newer"}})
    await admin.put("/api/sources/boom", json={"enabled": True})
    pv = (await admin.post("/api/backup/preview", json={"backup": first, "passphrase": PASS})).json()
    st = {r["key"]: r["status"] for r in pv["settings"]}
    assert st["locations"] == "changed" and st["source.boom"] == "removed" and st["ui"] == "same" and pv["will_change"] >= 2
    r = await admin.post("/api/backup/restore", json={"backup": first, "passphrase": PASS})
    assert r.status_code == 200 and "source.boom" in r.json()["changed"]
    core = app.state.core
    assert (await admin.get("/api/locations")).json() == LOCS
    assert await core.store.get("source.boom") is None                                           # not in the backup, so gone
    assert await core.store.get_secret("source.fake.api_key") == "k-original"                    # the backup's secret wins
    # undo
    assert (await admin.get("/api/backup")).json()["undo"]["settings"] >= 5
    snap = next((core.boot.data_dir / "backups").glob("pre-restore-*.json"))
    assert stat.S_IMODE(os.stat(snap).st_mode) == 0o600 and stat.S_IMODE(os.stat(snap.parent).st_mode) == 0o700
    assert "k-newer" not in snap.read_text()                                                     # secrets in it are encrypted, not plain
    r = await admin.post("/api/backup/undo")
    assert r.status_code == 200 and r.json()["undone"]
    assert (await admin.get("/api/locations")).json()[0]["id"] == "other"
    assert await core.store.get_secret("source.fake.api_key") == "k-newer"
    assert (await core.store.get("source.boom"))["enabled"] is True
    assert (await admin.get("/api/backup")).json()["undo"] is None
    assert (await admin.post("/api/backup/undo")).status_code == 422


async def test_only_the_newest_snapshots_are_kept(admin, app):
    doc = await export(admin, secrets=False)
    for _ in range(5):
        assert (await admin.post("/api/backup/restore", json={"backup": doc})).status_code == 200
    assert len(list((app.state.core.boot.data_dir / "backups").glob("pre-restore-*.json"))) == 3


async def test_restore_resets_the_page_policy_cache_and_is_audited_without_values(admin, app):
    await configure(admin)
    doc = await export(admin)
    core = app.state.core
    core.ui_cache = ("http://tiles:1",)
    await admin.post("/api/backup/restore", json={"backup": doc, "passphrase": PASS})
    assert core.ui_cache != ("http://tiles:1",)                              # dropped, then rebuilt from the restored map settings
    audit = await core.store.audit(50)
    assert {"export_backup", "restore_backup"} <= {a["action"] for a in audit}
    assert not any(s in json.dumps(audit) for s in ("k-original", "oidc-secret", PASS))


async def test_backup_is_for_admins_only(member, agent, admin):
    doc = await export(admin, secrets=False)
    for c in (member, agent):
        assert (await c.get("/api/backup")).status_code == 403
        assert (await c.post("/api/backup/export", json={})).status_code == 403
        assert (await c.post("/api/backup/preview", json={"backup": doc})).status_code == 403
        assert (await c.post("/api/backup/restore", json={"backup": doc})).status_code == 403
        assert (await c.post("/api/backup/undo")).status_code == 403
