"""Persistent SQLite repository for SSH connection metadata.

Passwords are intentionally excluded: they remain in the credential store or
in memory for the lifetime of the API process.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class SSHConnectionStore:
    def __init__(self, database: Path, legacy_json: Path):
        self.database = database
        self.legacy_json = legacy_json
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS ssh_connections (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    auth_type TEXT NOT NULL,
                    key_path TEXT NOT NULL,
                    remote_workdir TEXT NOT NULL,
                    scheduler TEXT NOT NULL,
                    setup_command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_tested_at TEXT,
                    last_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            count = connection.execute("SELECT COUNT(*) FROM ssh_connections").fetchone()[0]
            if count == 0 and self.legacy_json.exists():
                try:
                    legacy = json.loads(self.legacy_json.read_text(encoding="utf-8"))
                    if isinstance(legacy, list):
                        self._replace(connection, legacy)
                except (OSError, json.JSONDecodeError, KeyError, TypeError, sqlite3.Error):
                    pass

    @staticmethod
    def _row(connection: dict) -> tuple:
        return (
            connection["id"], connection["name"], connection["host"], connection["port"],
            connection["username"], connection["authType"], connection["keyPath"],
            connection["remoteWorkdir"], connection["scheduler"], connection["setupCommand"],
            connection["status"], connection.get("lastTestedAt"), connection["lastMessage"],
            connection["createdAt"], connection["updatedAt"],
        )

    @staticmethod
    def _replace(database: sqlite3.Connection, connections: list[dict]) -> None:
        database.execute("DELETE FROM ssh_connections")
        database.executemany("""
            INSERT INTO ssh_connections (
                id, name, host, port, username, auth_type, key_path, remote_workdir,
                scheduler, setup_command, status, last_tested_at, last_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [SSHConnectionStore._row(item) for item in connections])

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT id, name, host, port, username, auth_type, key_path, remote_workdir,
                       scheduler, setup_command, status, last_tested_at, last_message,
                       created_at, updated_at
                FROM ssh_connections ORDER BY created_at, id
            """).fetchall()
        return [{
            "id": row["id"], "name": row["name"], "host": row["host"], "port": row["port"],
            "username": row["username"], "authType": row["auth_type"], "keyPath": row["key_path"],
            "remoteWorkdir": row["remote_workdir"], "scheduler": row["scheduler"],
            "setupCommand": row["setup_command"], "status": row["status"],
            "lastTestedAt": row["last_tested_at"], "lastMessage": row["last_message"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        } for row in rows]

    def replace_all(self, connections: list[dict]) -> None:
        with self._connect() as database:
            self._replace(database, connections)
