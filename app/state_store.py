"""Local SQLite state store: tracks the sync watermark, which INQUIRIES have
already been pushed to GHL as notes, and which guest_card_id -> GHL contact
mappings exist, so re-runs don't create duplicate contacts or duplicate notes.

Schema note: `synced_guest_cards` is the legacy table from before we realized
guest_card_id identifies a PERSON (who can have many inquiries), not a single
inquiry. It's kept only for migration/backward-compat reads. New code should
use `synced_inquiries` (per-inquiry) and `contact_keys` (per-person contact
identity, keyed primarily by "gc:{guest_card_id}").
"""
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional


class StateStore:
    def __init__(self, db_path: str):
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            # Legacy table -- kept for migration/backward-compat reads only.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synced_guest_cards (
                    guest_card_id TEXT PRIMARY KEY,
                    ghl_contact_id TEXT NOT NULL,
                    ghl_note_id TEXT,
                    last_synced_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            # Contact identity: dedup_key -> ghl_contact_id.
            # Primary keys are "gc:{guest_card_id}" (one contact per person).
            # "email:..." / "phone:..." keys are also registered as a
            # secondary safety net in case AppFolio creates two guest cards
            # for the same real person.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_keys (
                    dedup_key TEXT PRIMARY KEY,
                    ghl_contact_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Per-INQUIRY sync tracking (the real unit of idempotency).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synced_inquiries (
                    inquiry_key TEXT PRIMARY KEY,
                    guest_card_id TEXT NOT NULL,
                    ghl_contact_id TEXT NOT NULL,
                    ghl_note_id TEXT,
                    synced_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_inquiries_gc_id "
                "ON synced_inquiries (guest_card_id)"
            )
            # Per-unit vacancy sync tracking. Unlike synced_inquiries, this is
            # always upserted (not skip-if-seen) since fields like days_vacant
            # legitimately change on every run.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synced_vacancies (
                    vacancy_key TEXT PRIMARY KEY,
                    ghl_record_id TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                )
                """
            )
            # Round-robin leasing agent roster (replaces AppFolio's own
            # round-robin, which is being sunset). `sort_order` defines
            # rotation order; `active` lets an agent be paused without
            # losing their position/history.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS round_robin_agents (
                    name TEXT PRIMARY KEY,
                    sort_order INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            # Generic settings: 'enabled' ('true'/'false'), 'last_agent'
            # (name of the last agent assigned, so rotation resumes correctly
            # across runs).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS round_robin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

    # ---- watermark -------------------------------------------------------

    def get_watermark(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'last_received_on_to'"
            ).fetchone()
            return row["value"] if row else None

    def set_watermark(self, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES ('last_received_on_to', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (value,),
            )

    # ---- contact identity (guest_card_id / email / phone -> ghl_contact_id) --

    def get_contact_by_key(self, dedup_key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ghl_contact_id FROM contact_keys WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            return row["ghl_contact_id"] if row else None

    def upsert_contact_key(self, dedup_key: str, ghl_contact_id: str, updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO contact_keys (dedup_key, ghl_contact_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    ghl_contact_id = excluded.ghl_contact_id,
                    updated_at = excluded.updated_at
                """,
                (dedup_key, ghl_contact_id, updated_at),
            )

    # ---- per-inquiry sync tracking ----------------------------------------

    def get_synced_inquiry(self, inquiry_key: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM synced_inquiries WHERE inquiry_key = ?",
                (inquiry_key,),
            ).fetchone()

    def upsert_synced_inquiry(
        self,
        inquiry_key: str,
        guest_card_id: str,
        ghl_contact_id: str,
        ghl_note_id: Optional[str],
        synced_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO synced_inquiries
                    (inquiry_key, guest_card_id, ghl_contact_id, ghl_note_id, synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(inquiry_key) DO UPDATE SET
                    guest_card_id = excluded.guest_card_id,
                    ghl_contact_id = excluded.ghl_contact_id,
                    ghl_note_id = excluded.ghl_note_id,
                    synced_at = excluded.synced_at
                """,
                (inquiry_key, guest_card_id, ghl_contact_id, ghl_note_id, synced_at),
            )

    # ---- per-unit vacancy sync tracking ------------------------------------

    def get_synced_vacancy(self, vacancy_key: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM synced_vacancies WHERE vacancy_key = ?",
                (vacancy_key,),
            ).fetchone()

    def all_synced_vacancies(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT vacancy_key, ghl_record_id FROM synced_vacancies"
            ).fetchall()

    def delete_synced_vacancy(self, vacancy_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM synced_vacancies WHERE vacancy_key = ?",
                (vacancy_key,),
            )

    def upsert_synced_vacancy(self, vacancy_key: str, ghl_record_id: str, synced_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO synced_vacancies (vacancy_key, ghl_record_id, synced_at)
                VALUES (?, ?, ?)
                ON CONFLICT(vacancy_key) DO UPDATE SET
                    ghl_record_id = excluded.ghl_record_id,
                    synced_at = excluded.synced_at
                """,
                (vacancy_key, ghl_record_id, synced_at),
            )

    # ---- legacy table access (migration only) -----------------------------

    def legacy_guest_cards(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM synced_guest_cards").fetchall()

    # ---- round-robin agent roster -----------------------------------------

    def is_round_robin_enabled(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM round_robin_settings WHERE key = 'enabled'"
            ).fetchone()
            return row is not None and row["value"] == "true"

    def set_round_robin_enabled(self, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO round_robin_settings (key, value) VALUES ('enabled', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("true" if enabled else "false",),
            )

    def list_round_robin_agents(self, active_only: bool = True) -> list[str]:
        """Returns agent names in rotation order."""
        with self._connect() as conn:
            query = "SELECT name FROM round_robin_agents"
            if active_only:
                query += " WHERE active = 1"
            query += " ORDER BY sort_order ASC"
            return [row["name"] for row in conn.execute(query).fetchall()]

    def all_round_robin_agents(self) -> list[sqlite3.Row]:
        """Returns all agents (active and inactive) in rotation order, for display."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM round_robin_agents ORDER BY sort_order ASC"
            ).fetchall()

    def add_round_robin_agent(self, name: str) -> None:
        """Adds a new agent at the end of the rotation, or reactivates them
        (at their existing position) if they were previously removed."""
        with self._connect() as conn:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM round_robin_agents"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO round_robin_agents (name, sort_order, active)
                VALUES (?, ?, 1)
                ON CONFLICT(name) DO UPDATE SET active = 1
                """,
                (name, max_order + 1),
            )

    def remove_round_robin_agent(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM round_robin_agents WHERE name = ?", (name,))

    def set_round_robin_agent_active(self, name: str, active: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE round_robin_agents SET active = ? WHERE name = ?",
                (1 if active else 0, name),
            )

    def reorder_round_robin_agents(self, ordered_names: list[str]) -> None:
        with self._connect() as conn:
            for index, name in enumerate(ordered_names):
                conn.execute(
                    "UPDATE round_robin_agents SET sort_order = ? WHERE name = ?",
                    (index, name),
                )

    def get_last_round_robin_agent(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM round_robin_settings WHERE key = 'last_agent'"
            ).fetchone()
            return row["value"] if row else None

    def set_last_round_robin_agent(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO round_robin_settings (key, value) VALUES ('last_agent', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (name,),
            )
