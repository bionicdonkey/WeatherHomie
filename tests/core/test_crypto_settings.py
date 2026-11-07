import pytest
from sqlalchemy import select

from stormtracker.settings import SecretBox, SettingsStore, WrongMasterKey
from stormtracker.storage.db import Database
from stormtracker.storage.tables import AuditLog, Secret


def test_roundtrip_and_aad_binding():
    box = SecretBox("a" * 40)
    blob = box.encrypt("slot1", "hunter2")
    assert b"hunter2" not in blob
    assert box.decrypt("slot1", blob) == "hunter2"
    with pytest.raises(WrongMasterKey):
        box.decrypt("slot2", blob)            # ciphertext can't be moved to another slot
    with pytest.raises(WrongMasterKey):
        SecretBox("b" * 40).decrypt("slot1", blob)


def test_nonce_random():
    box = SecretBox("a" * 40)
    assert box.encrypt("s", "x") != box.encrypt("s", "x")


async def test_store_secret_encrypted_at_rest_and_not_audited(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/s.db")
    await db.create_all()
    store = SettingsStore(db, SecretBox("a" * 40))
    await store.init()
    await store.set_secret("x.key", "SUPERSECRET")
    assert await store.get_secret("x.key") == "SUPERSECRET"
    async with db.session() as s:
        blob = (await s.get(Secret, "x.key")).blob
        audit = [str(r.detail) for r in (await s.execute(select(AuditLog))).scalars()]
    assert b"SUPERSECRET" not in blob
    assert not any("SUPERSECRET" in a for a in audit)
    await db.dispose()


async def test_wrong_master_key_detected(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/s.db"
    db = Database(url)
    await db.create_all()
    await SettingsStore(db, SecretBox("a" * 40)).init()
    with pytest.raises(WrongMasterKey):
        await SettingsStore(db, SecretBox("b" * 40)).init()
    await db.dispose()


async def test_settings_versioning(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/s.db")
    await db.create_all()
    st = SettingsStore(db, SecretBox("a" * 40))
    await st.set("k", {"a": 1})
    await st.set("k", {"a": 2})
    assert await st.get("k") == {"a": 2}
    assert await st.get("missing", 5) == 5
    await db.dispose()
