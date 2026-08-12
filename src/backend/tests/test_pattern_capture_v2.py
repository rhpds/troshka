"""Tests for Pattern Capture v2 — OBC-based local RGW capture."""

import uuid

import pytest

from app.models.provider import Provider
from app.models.user import User
from app.services.s3_storage import get_cluster_s3_config
from tests.conftest import TestSession


@pytest.fixture
def db():
    session = TestSession()
    yield session
    session.query(Provider).filter(Provider.name.like("test-%")).delete()
    session.query(User).filter(User.email == "v2test@test.com").delete()
    session.commit()
    session.close()


@pytest.fixture
def user(db):
    u = User(id=str(uuid.uuid4()), email="v2test@test.com", display_name="V2 Test")
    db.add(u)
    db.commit()
    return u


class TestGetClusterS3Config:
    def test_returns_s3_config_from_provider_credentials(self, db):
        import json

        s3_cfg = {
            "bucket": "troshka-patterns-abc",
            "endpoint": "http://rgw.svc:80",
            "access_key_id": "AK123",
            "secret_access_key": "SK456",
            "region": "us-east-1",
        }
        provider = Provider(
            id=str(uuid.uuid4()),
            name="test-kubevirt",
            type="kubevirt_native",
            state="active",
            credentials=json.dumps({"s3_config": s3_cfg}),
        )
        db.add(provider)
        db.commit()

        result = get_cluster_s3_config(db, provider.id)
        assert result is not None
        assert result["bucket"] == "troshka-patterns-abc"
        assert result["endpoint"] == "http://rgw.svc:80"

    def test_returns_none_for_provider_without_s3_config(self, db):
        import json

        provider = Provider(
            id=str(uuid.uuid4()),
            name="test-aws",
            type="aws",
            state="active",
            credentials=json.dumps({"region": "us-east-1"}),
        )
        db.add(provider)
        db.commit()

        result = get_cluster_s3_config(db, provider.id)
        assert result is None

    def test_returns_none_for_missing_provider(self, db):
        result = get_cluster_s3_config(db, str(uuid.uuid4()))
        assert result is None

    def test_returns_none_for_provider_with_no_credentials(self, db):
        provider = Provider(
            id=str(uuid.uuid4()),
            name="test-empty",
            type="kubevirt_native",
            state="active",
        )
        db.add(provider)
        db.commit()

        result = get_cluster_s3_config(db, provider.id)
        assert result is None


class TestBuildExportJobS3Tuning:
    """Verify the export Job uses the correct S3 tuning parameters."""

    @pytest.fixture(autouse=True)
    def _load_patterns_module(self):
        import importlib.util
        import os

        spec = importlib.util.spec_from_file_location(
            "helpers.patterns",
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "operator",
                "helpers",
                "patterns.py",
            ),
        )
        mod = importlib.util.module_from_spec(spec)
        # Mock the helpers.k8s import that patterns.py needs
        import sys
        import types

        mock_k8s = types.ModuleType("helpers.k8s")
        mock_k8s.TOOLS_IMAGE = "test-image:latest"
        old = sys.modules.get("helpers.k8s")
        sys.modules["helpers.k8s"] = mock_k8s
        try:
            spec.loader.exec_module(mod)
        finally:
            if old is not None:
                sys.modules["helpers.k8s"] = old
            else:
                sys.modules.pop("helpers.k8s", None)
        self._build_export_job = mod.build_export_job

    def test_export_job_uses_256mb_chunks(self):
        job = self._build_export_job(
            "test-job",
            "test-ns",
            "temp-pvc",
            "patterns/test/disk.qcow2",
            {
                "bucket": "b",
                "endpoint": "http://rgw:80",
                "region": "us-east-1",
                "credentialsSecret": "s3-creds",
            },
            100,
        )
        cmd = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
        assert "256MB" in cmd
        assert "max_concurrent_requests 7" in cmd

    def test_export_job_sets_home_to_scratch(self):
        job = self._build_export_job(
            "test-job",
            "test-ns",
            "temp-pvc",
            "patterns/test/disk.qcow2",
            {
                "bucket": "b",
                "endpoint": "http://rgw:80",
                "region": "us-east-1",
                "credentialsSecret": "s3-creds",
            },
            100,
        )
        cmd = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
        assert "HOME=/scratch" in cmd
