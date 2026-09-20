"""Per-installation BYO-key storage.

One Carlos server process serves every installation, so unlike the GitHub
Actions script (which reads a repo secret env var), the App needs somewhere
to persist "installation 12345 uses provider=claude with this key". Keys are
encrypted at rest with Fernet (WB-SEC-01: no plaintext secrets in storage);
KEY_ENCRYPTION_SECRET (see config.py) is the encryption key, kept only in the
server's own environment, never in the database.

SQLite is enough for a single-instance deployment. If Carlos ever needs to
run as multiple replicas, swap this module's internals for Postgres — the
set_key/get_key/delete_key interface below doesn't need to change.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS installation_keys (
    installation_id INTEGER PRIMARY KEY,
    provider        TEXT NOT NULL,
    encrypted_key   BLOB NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass(frozen=True)
class InstallationKey:
    provider: str
    api_key: str


@contextmanager
def _connect():
    settings = get_settings()
    conn = sqlite3.connect(settings.database_path, timeout=30)
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fernet() -> Fernet:
    return Fernet(get_settings().key_encryption_key.encode("utf-8"))


def set_key(installation_id: int, provider: str, api_key: str) -> None:
    """Encrypt and store (or replace) the key for an installation."""
    encrypted = _fernet().encrypt(api_key.encode("utf-8"))
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO installation_keys (installation_id, provider, encrypted_key, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(installation_id) DO UPDATE SET
                provider = excluded.provider,
                encrypted_key = excluded.encrypted_key,
                updated_at = excluded.updated_at
            """,
            (installation_id, provider.strip().lower(), encrypted),
        )


def get_key(installation_id: int) -> InstallationKey | None:
    """Return the decrypted key for an installation, or None if it hasn't
    configured one yet. Returns None (rather than raising) on decryption
    failure too — e.g. after a KEY_ENCRYPTION_SECRET rotation — since the
    caller's correct response either way is "ask the admin to reconfigure",
    not a 500.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT provider, encrypted_key FROM installation_keys WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
    if row is None:
        return None
    provider, encrypted = row
    try:
        api_key = _fernet().decrypt(encrypted).decode("utf-8")
    except InvalidToken:
        return None
    return InstallationKey(provider=provider, api_key=api_key)


def delete_key(installation_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM installation_keys WHERE installation_id = ?", (installation_id,))
