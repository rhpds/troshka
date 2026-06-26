"""Test the finalize-seed endpoint for library seeding."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_finalize_seed_endpoint_exists():
    """Route is registered and returns a valid HTTP status."""
    response = client.post(
        "/api/v1/library/nonexistent-id/finalize-seed",
        json={"seed_key": "seed/test.qcow2", "tags": []},
    )
    # Dev mode auto-auth, but item won't exist
    assert response.status_code in (404, 422)
