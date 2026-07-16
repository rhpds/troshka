"""Agent CA service — manages the global mTLS CA for backend-to-agent communication."""

import datetime
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_cert_paths_cache: tuple[str, str] | None = None


def ensure_agent_ca():
    """Generate CA + client cert if they don't exist yet. Idempotent."""
    from app.core.database import SessionLocal
    from app.models.system_config import SystemConfig

    db = SessionLocal()
    try:
        existing = db.query(SystemConfig).filter_by(key="agent_ca_cert").first()
        if existing:
            logger.info("Agent mTLS CA already exists")
            return
        _generate_and_store(db)
    finally:
        db.close()


def _generate_and_store(db):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from app.models.system_config import SystemConfig

    # Generate CA (RSA 4096, 10 years)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "troshka-agent-ca"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Troshka"),
        ]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # Generate client cert for backend (RSA 2048, 1 year, signed by CA)
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "troshka-backend"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Troshka"),
        ]
    )
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_subject)
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    ca_key_pem = ca_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    client_cert_pem = client_cert.public_bytes(serialization.Encoding.PEM).decode()
    client_key_pem = client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    for k, v in [
        ("agent_ca_cert", ca_cert_pem),
        ("agent_ca_key", ca_key_pem),
        ("agent_client_cert", client_cert_pem),
        ("agent_client_key", client_key_pem),
    ]:
        db.add(SystemConfig(key=k, value=v))
    db.commit()
    logger.info("Generated agent mTLS CA and client certificate")


def get_agent_ca_cert() -> str:
    """Return the CA cert PEM for deploying to agents. Empty string if not yet generated."""
    from app.core.database import SessionLocal
    from app.models.system_config import SystemConfig

    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter_by(key="agent_ca_cert").first()
        return row.value if row else ""
    finally:
        db.close()


def get_client_cert_paths() -> tuple[str | None, str | None]:
    """Return (cert_path, key_path) for the backend client cert. Cached per process."""
    global _cert_paths_cache
    if _cert_paths_cache:
        cert_path, key_path = _cert_paths_cache
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return _cert_paths_cache

    from app.core.database import SessionLocal
    from app.models.system_config import SystemConfig

    db = SessionLocal()
    try:
        cert_row = db.query(SystemConfig).filter_by(key="agent_client_cert").first()
        key_row = db.query(SystemConfig).filter_by(key="agent_client_key").first()
        if not cert_row or not key_row:
            return None, None

        cert_file = tempfile.NamedTemporaryFile(
            mode="w", suffix="-client.crt", prefix="troshka-", delete=False
        )
        cert_file.write(cert_row.value)
        cert_file.close()

        key_file = tempfile.NamedTemporaryFile(
            mode="w", suffix="-client.key", prefix="troshka-", delete=False
        )
        key_file.write(key_row.value)
        key_file.close()
        os.chmod(key_file.name, 0o600)

        _cert_paths_cache = (cert_file.name, key_file.name)
        return _cert_paths_cache
    finally:
        db.close()
