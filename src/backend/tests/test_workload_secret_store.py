from app.models.system_config import SystemConfig
from app.services.workloads import secret_store
from tests.conftest import TestSession


def test_set_get_roundtrip():
    db = TestSession()
    try:
        secret_store.set_secret(db, "vault_key_t1", "s3cr3t-pass")
        assert secret_store.get_secret(db, "vault_key_t1") == "s3cr3t-pass"
    finally:
        db.close()


def test_stored_value_is_encrypted_at_rest():
    db = TestSession()
    try:
        secret_store.set_secret(db, "vault_key_t2", "plaintext-value")
        row = db.get(SystemConfig, "workload.secret.vault_key_t2")
        assert row is not None
        assert row.value != "plaintext-value"  # ciphertext, not plaintext
    finally:
        db.close()


def test_update_overwrites():
    db = TestSession()
    try:
        secret_store.set_secret(db, "k_t3", "one")
        secret_store.set_secret(db, "k_t3", "two")
        assert secret_store.get_secret(db, "k_t3") == "two"
    finally:
        db.close()


def test_get_missing_returns_none():
    db = TestSession()
    try:
        assert secret_store.get_secret(db, "does-not-exist-t4") is None
    finally:
        db.close()


def test_delete_removes_secret():
    db = TestSession()
    try:
        secret_store.set_secret(db, "k_t5", "v")
        secret_store.delete_secret(db, "k_t5")
        assert secret_store.get_secret(db, "k_t5") is None
    finally:
        db.close()


def test_list_secret_names_includes_set_names():
    db = TestSession()
    try:
        secret_store.set_secret(db, "alpha_t6", "v")
        secret_store.set_secret(db, "beta_t6", "v")
        names = secret_store.list_secret_names(db)
        assert "alpha_t6" in names
        assert "beta_t6" in names
    finally:
        db.close()


def test_json_secret_roundtrip():
    db = TestSession()
    try:
        creds = {"url": "https://github.com/rhpds/agnosticv.git", "token": "abc"}
        secret_store.set_json_secret(db, "agnosticv_git_t7", creds)
        assert secret_store.get_json_secret(db, "agnosticv_git_t7") == creds
        # and it is stored encrypted, not as readable json
        row = db.get(SystemConfig, "workload.secret.agnosticv_git_t7")
        assert "github.com" not in row.value
    finally:
        db.close()
