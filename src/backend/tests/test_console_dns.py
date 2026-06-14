import time


def test_sign_console_jwt_contains_required_claims():
    from app.services.console_dns import sign_console_jwt

    token = sign_console_jwt(
        domain_name="troshka-abcd1234-efgh5678",
        host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        secret="test-secret-token-hex-value",
    )
    assert isinstance(token, str)
    assert len(token) > 0

    import base64
    import json

    parts = token.split(".")
    assert len(parts) == 3
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    assert payload["domain_name"] == "troshka-abcd1234-efgh5678"
    assert payload["host_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "exp" in payload
    assert payload["exp"] > time.time()
    assert payload["exp"] <= time.time() + 301


def test_sign_console_jwt_different_secrets_produce_different_tokens():
    from app.services.console_dns import sign_console_jwt

    t1 = sign_console_jwt("dom", "host1", "secret-a")
    t2 = sign_console_jwt("dom", "host1", "secret-b")
    assert t1 != t2


def test_verify_console_jwt_valid():
    from app.services.console_dns import sign_console_jwt, verify_console_jwt

    secret = "my-test-secret"
    token = sign_console_jwt("troshka-abcd-efgh", "host-id-1", secret)
    claims = verify_console_jwt(token, secret)
    assert claims["domain_name"] == "troshka-abcd-efgh"
    assert claims["host_id"] == "host-id-1"


def test_verify_console_jwt_wrong_secret():
    from app.services.console_dns import sign_console_jwt, verify_console_jwt

    token = sign_console_jwt("dom", "host", "correct-secret")
    result = verify_console_jwt(token, "wrong-secret")
    assert result is None


def test_verify_console_jwt_expired():
    import base64
    import hashlib
    import hmac
    import json

    from app.services.console_dns import verify_console_jwt

    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload_data = {"domain_name": "dom", "host_id": "h", "exp": int(time.time()) - 10}
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_data).encode())
        .rstrip(b"=")
        .decode()
    )
    sig_input = f"{header}.{payload}".encode()
    sig = (
        base64.urlsafe_b64encode(
            hmac.new(b"secret", sig_input, hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    token = f"{header}.{payload}.{sig}"

    result = verify_console_jwt(token, "secret")
    assert result is None


def test_console_domain_from_instance_id():
    from app.services.console_dns import console_domain_for_host

    fqdn = console_domain_for_host("i-0abc123def456", "tc.rhdp.net")
    assert fqdn == "i-0abc123def456.tc.rhdp.net"
