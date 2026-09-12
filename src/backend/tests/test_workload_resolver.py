import pytest

from app.services.workloads import agnosticv, repo_cache, resolver, secret_store
from tests.conftest import TestSession


def _seed_secrets(db):
    secret_store.set_secret(db, "vault_key", "testpass")
    secret_store.set_json_secret(
        db, "agnosticv_git", {"url": "https://example.com/agnosticv.git"}
    )


def test_resolve_catalog_item_happy_path(monkeypatch):
    db = TestSession()
    try:
        _seed_secrets(db)
        monkeypatch.setattr(repo_cache, "ensure_agnosticv", lambda _url: "/fake/root")
        monkeypatch.setattr(
            agnosticv, "resolve_path", lambda _root, _cid: "agd_v2/x/prod.yaml"
        )
        monkeypatch.setattr(
            agnosticv,
            "merge_path",
            lambda _root, _rel: {
                "k": "$ANSIBLE_VAULT-marker",
                "__meta__": {
                    "deployer": {
                        "scm_ref": "main",
                        "execution_environment": {"image": "ee:1"},
                    }
                },
            },
        )
        monkeypatch.setattr(
            agnosticv,
            "decrypt_vault_strings",
            lambda data, pw: {**data, "k": "decrypted"} if pw == "testpass" else data,
        )
        item = resolver.resolve_catalog_item(db, "agd-v2.x.prod")
        assert item.ee_image == "ee:1"
        assert item.scm_ref == "main"
        assert item.extra_vars["k"] == "decrypted"
    finally:
        db.close()


def test_resolve_requires_vault_key(monkeypatch):
    db = TestSession()
    try:
        secret_store.delete_secret(db, "vault_key")
        secret_store.set_json_secret(db, "agnosticv_git", {"url": "u"})
        monkeypatch.setattr(repo_cache, "ensure_agnosticv", lambda _url: "/fake/root")
        with pytest.raises(resolver.ResolverError):
            resolver.resolve_catalog_item(db, "agd-v2.x.prod")
    finally:
        db.close()


def test_resolve_requires_git_config(monkeypatch):
    db = TestSession()
    try:
        secret_store.set_secret(db, "vault_key", "testpass")
        secret_store.delete_secret(db, "agnosticv_git")
        monkeypatch.setattr(repo_cache, "ensure_agnosticv", lambda _url: "/fake/root")
        with pytest.raises(resolver.ResolverError):
            resolver.resolve_catalog_item(db, "agd-v2.x.prod")
    finally:
        db.close()
