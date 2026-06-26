"""Test the upload proxy endpoint for MinIO."""


def test_upload_proxy_route_exists():
    """Verify upload_proxy endpoint is registered in the router."""
    from app.api.library import router

    # Check that the upload-proxy route is registered
    # Routes don't include the prefix, so we check for /{item_id}/upload-proxy
    route_found = False
    for route in router.routes:
        if "upload-proxy" in route.path:
            route_found = True
            assert "POST" in route.methods
            break
    assert route_found, "upload-proxy route not found in router"
