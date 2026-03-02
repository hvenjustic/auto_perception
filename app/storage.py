from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from app.models import PersistedState


class StateStore:
    def __init__(self, db_file: str, legacy_state_file: str | None = None) -> None:
        self.db_file = Path(db_file)
        self.legacy_state_file = Path(legacy_state_file) if legacy_state_file else None
        self._lock = threading.Lock()
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")

        with self._lock:
            self._init_schema_unlocked()
            self._migrate_from_legacy_json_unlocked()

    def load(self) -> PersistedState:
        with self._lock:
            return self._read_unlocked()

    def save(self, state: PersistedState) -> None:
        with self._lock:
            self._write_unlocked(state)

    def _read_unlocked(self) -> PersistedState:
        row = self._conn.execute(
            "SELECT payload FROM app_state WHERE state_key = ?",
            ("global",),
        ).fetchone()
        raw = row[0] if row else "{}"
        data = json.loads(raw) if raw.strip() else {}
        return PersistedState.model_validate(data)

    def _write_unlocked(self, state: PersistedState) -> None:
        payload = state.model_dump(mode="json")
        serialized = json.dumps(payload, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO app_state(state_key, payload)
            VALUES(?, ?)
            ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload
            """,
            ("global", serialized),
        )
        self._conn.commit()

    def _init_schema_unlocked(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                state_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _migrate_from_legacy_json_unlocked(self) -> None:
        if not self.legacy_state_file:
            return
        if not self.legacy_state_file.exists():
            return

        row = self._conn.execute(
            "SELECT payload FROM app_state WHERE state_key = ?",
            ("global",),
        ).fetchone()
        if row and row[0] and row[0].strip() and row[0].strip() != "{}":
            return

        raw = self.legacy_state_file.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        state = PersistedState.model_validate(data)
        self._write_unlocked(state)
