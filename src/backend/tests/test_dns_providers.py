import os
os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy.dialects import sqlite
sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from tests.conftest import TestSession


def test_create_dns_provider():
    from app.models.dns_provider import DnsProvider

    db = TestSession()
    provider = DnsProvider(
        name="Test BIND",
        type="nsupdate",
        config={
            "server": "10.0.0.53",
            "port": 53,
            "key_name": "update-key",
            "key_secret": "secret123",
            "key_algorithm": "hmac-sha256",
            "default_zone": "example.com",
        },
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    assert provider.id is not None
    assert len(provider.id) == 36
    assert provider.name == "Test BIND"
    assert provider.type == "nsupdate"
    assert provider.config["server"] == "10.0.0.53"
    assert provider.config["default_zone"] == "example.com"
    db.delete(provider)
    db.commit()
    db.close()
