"""Admin-central secret storage for the workloads subsystem.

Secrets (git credentials, the Ansible Vault key, named cloud-cred sets) are kept
encrypted at rest in the existing ``system_config`` key/value table, namespaced
under ``workload.secret.``. Values are Fernet-encrypted via
``app.core.encryption`` (the same mechanism used for pull secrets).
"""

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.encryption import decrypt, encrypt
from app.models.system_config import SystemConfig

_PREFIX = "workload.secret."


def _key(name: str) -> str:
    return _PREFIX + name


def set_secret(db: Session, name: str, value: str) -> None:
    row = db.get(SystemConfig, _key(name))
    ciphertext = encrypt(value)
    if row is None:
        db.add(SystemConfig(key=_key(name), value=ciphertext))
    else:
        row.value = ciphertext
    db.commit()


def get_secret(db: Session, name: str) -> str | None:
    row = db.get(SystemConfig, _key(name))
    if row is None:
        return None
    return decrypt(row.value)


def delete_secret(db: Session, name: str) -> None:
    row = db.get(SystemConfig, _key(name))
    if row is not None:
        db.delete(row)
        db.commit()


def list_secret_names(db: Session) -> list[str]:
    rows = db.query(SystemConfig).filter(SystemConfig.key.like(_PREFIX + "%")).all()
    return sorted(r.key[len(_PREFIX) :] for r in rows)


def set_json_secret(db: Session, name: str, obj: Any) -> None:
    set_secret(db, name, json.dumps(obj))


def get_json_secret(db: Session, name: str) -> Any:
    raw = get_secret(db, name)
    if not raw:
        return None
    return json.loads(raw)
