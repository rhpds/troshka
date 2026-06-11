import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import config


def _get_fernet() -> Fernet:
    secret = config.auth.jwt_secret
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""
