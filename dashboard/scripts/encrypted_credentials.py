"""Encrypted SSH passwords persisted in the local SQLite connection database."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialStore:
    """Store ciphertext in SQLite; keep the per-user encryption key separate."""

    def __init__(self, database: Path, key_file: Path):
        self.database = database
        self.key_file = key_file
        self._cipher = Fernet(self._load_or_create_key())
        self._initialize()

    def _load_or_create_key(self) -> bytes:
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        if self.key_file.exists():
            key = self.key_file.read_bytes().strip()
            try:
                Fernet(key)
                return key
            except ValueError as exc:
                raise RuntimeError("La clave local de cifrado SSH no es válida.") from exc
        key = Fernet.generate_key()
        temporary = self.key_file.with_suffix(".tmp")
        temporary.write_bytes(key + b"\n")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.key_file)
        return key

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS ssh_secrets (
                    connection_id TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL
                )
            """)
        if os.name != "nt":
            self.database.chmod(0o600)

    def save(self, connection_id: str, password: str) -> None:
        if not connection_id.isalnum():
            raise ValueError("Identificador inválido.")
        ciphertext = self._cipher.encrypt(password.encode("utf-8"))
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO ssh_secrets (connection_id, ciphertext) VALUES (?, ?) "
                "ON CONFLICT(connection_id) DO UPDATE SET ciphertext = excluded.ciphertext",
                (connection_id, ciphertext),
            )

    def load(self, connection_id: str) -> str | None:
        if not connection_id.isalnum():
            raise ValueError("Identificador inválido.")
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT ciphertext FROM ssh_secrets WHERE connection_id = ?", (connection_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._cipher.decrypt(row[0]).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("No se pudo descifrar la contraseña SSH guardada.") from exc

    def delete(self, connection_id: str) -> None:
        if not connection_id.isalnum():
            raise ValueError("Identificador inválido.")
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM ssh_secrets WHERE connection_id = ?", (connection_id,))
