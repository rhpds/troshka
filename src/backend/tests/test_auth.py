from app.core.auth import create_jwt, decode_jwt, hash_password, verify_password


def test_password_hashing():
    hashed = hash_password("secret123")
    assert hashed != "secret123"
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)


def test_jwt_roundtrip():
    token = create_jwt(user_id="abc-123", email="test@example.com", role="user")
    payload = decode_jwt(token)
    assert payload["sub"] == "abc-123"
    assert payload["email"] == "test@example.com"
    assert payload["role"] == "user"


def test_jwt_invalid_token():
    payload = decode_jwt("garbage.token.here")
    assert payload is None
