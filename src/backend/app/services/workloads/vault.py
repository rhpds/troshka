"""Decrypt Ansible Vault (VAULT;1.1;AES256) values without ansible-core.

Ansible Vault format:
  line 0: ``$ANSIBLE_VAULT;1.1;AES256``
  body:   hex of an ASCII string ``<salt_hex>\\n<hmac_hex>\\n<ciphertext_hex>``.
Key material is PBKDF2-HMAC-SHA256(password, salt, 10000, 80 bytes) split into
cipher key (32), HMAC key (32), IV (16). Integrity is HMAC-SHA256 over the
ciphertext; content is AES-256-CTR with PKCS7 padding.
"""

from binascii import unhexlify

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, hmac, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_HEADER = "$ANSIBLE_VAULT"
_ITERATIONS = 10000
_KEYLEN = 80  # 32 cipher + 32 hmac + 16 iv


class VaultError(Exception):
    pass


def is_vault(text: str) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(_HEADER)


def _split_envelope(vaulttext: str) -> tuple[bytes, bytes, bytes]:
    lines = vaulttext.strip().splitlines()
    body = "".join(line.strip() for line in lines[1:])
    try:
        decoded = unhexlify(body).decode("ascii")
        salt_hex, hmac_hex, ct_hex = decoded.split("\n")
        return unhexlify(salt_hex), unhexlify(hmac_hex), unhexlify(ct_hex)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VaultError(f"malformed vault payload: {exc}") from exc


def decrypt_vault(vaulttext: str, password: str) -> str:
    if not is_vault(vaulttext):
        raise VaultError("not an ansible-vault value")
    salt, expected_hmac, ciphertext = _split_envelope(vaulttext)
    keymat = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=_KEYLEN, salt=salt, iterations=_ITERATIONS
    ).derive(password.encode("utf-8"))
    cipher_key, hmac_key, iv = keymat[:32], keymat[32:64], keymat[64:80]

    verifier = hmac.HMAC(hmac_key, hashes.SHA256())
    verifier.update(ciphertext)
    try:
        verifier.verify(expected_hmac)
    except InvalidSignature as exc:
        raise VaultError("vault HMAC verification failed (wrong password?)") from exc

    decryptor = Cipher(algorithms.AES(cipher_key), modes.CTR(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")
