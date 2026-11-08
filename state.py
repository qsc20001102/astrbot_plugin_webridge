"""Small transactional journal: IDs/receipts only, never tokens or message bodies.

The AstrBot queue cannot participate in a database transaction. A crash after
handoff is therefore retained for review rather than automatically handed off twice.
"""

import sqlite3
from pathlib import Path


def cursor_number(value: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or int(value) > 9223372036854775807
    ):
        raise ValueError("invalid_cursor")
    return int(value)


class StateIdentityError(RuntimeError):
    """Stored identity changed; preserve evidence and require operator inspection."""


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, cursor TEXT NOT NULL);
            INSERT OR IGNORE INTO checkpoint VALUES (1, '0');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recalls (
                thread TEXT NOT NULL, server_id TEXT NOT NULL, PRIMARY KEY(thread, server_id));
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, server_id TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sends (
                key TEXT PRIMARY KEY, message_id TEXT, thread TEXT NOT NULL,
                state TEXT NOT NULL, send_id TEXT);
            CREATE INDEX IF NOT EXISTS sends_message_state ON sends(message_id, state);
            CREATE TABLE IF NOT EXISTS send_chunks (
                key TEXT NOT NULL, part INTEGER NOT NULL, state TEXT NOT NULL,
                duration_ms INTEGER NOT NULL, server_id TEXT,
                PRIMARY KEY(key, part));
        """)
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='partial' WHERE status IN ('queued','needs_review') "
                "AND EXISTS (SELECT 1 FROM sends JOIN send_chunks USING(key) "
                "WHERE sends.message_id=messages.id AND sends.state!='accepted' "
                "AND send_chunks.state='accepted')"
            )
            self.db.execute("UPDATE messages SET status='needs_review' WHERE status='queued'")
            self.db.execute("UPDATE sends SET state='needs_review' WHERE state='submitting'")
            self.db.execute(
                "UPDATE messages SET status=CASE WHEN status='replied' THEN 'partial' "
                "ELSE 'needs_review' END WHERE status IN ('replied','no_reply') "
                "AND EXISTS (SELECT 1 FROM sends WHERE sends.message_id=messages.id "
                "AND sends.state!='accepted')"
            )

    def bind_server(self, server_id: str):
        if not isinstance(server_id, str) or not server_id:
            raise StateIdentityError("Native server identity unavailable; state retained")
        row = self.db.execute("SELECT value FROM settings WHERE key='server_id'").fetchone()
        if row and row[0] != server_id:
            raise StateIdentityError("Native server identity changed; state retained for review")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('server_id', ?)", (server_id,))

    @property
    def cursor(self) -> str:
        return self.db.execute("SELECT cursor FROM checkpoint WHERE id=1").fetchone()[0]

    def _advance(self, cursor: str):
        if cursor_number(cursor) > cursor_number(self.cursor):
            self.db.execute("UPDATE checkpoint SET cursor=? WHERE id=1", (cursor,))

    def advance(self, cursor: str):
        with self.db:
            self._advance(cursor)

    def accept(
        self, cursor: str, message_id: str, thread: str, server_id: str | None, status: str
    ) -> bool:
        existing = self.db.execute(
            "SELECT thread, server_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if existing and (existing[0] != thread or existing[1] != server_id):
            self.mark_message(message_id, "needs_review")
            raise StateIdentityError("Native message identity reused; cursor retained for review")
        with self.db:
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?", (thread, server_id)
            ).fetchone()
            result = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                (message_id, thread, server_id, "recalled" if recalled else status),
            )
            self._advance(cursor)
            return result.rowcount == 1 and not recalled

    def message_status(self, message_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def message_matches(self, message_id: str, thread: str, server_id: str | None) -> bool:
        row = self.db.execute(
            "SELECT thread, server_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        return bool(row and row[0] == thread and row[1] == server_id)

    def find_message_id(self, thread: str, server_id: str) -> str | None:
        rows = self.db.execute(
            "SELECT id FROM messages WHERE thread=? AND server_id=? LIMIT 2", (thread, server_id)
        ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def mark_message(self, message_id: str, status: str):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status=CASE "
                "WHEN status='partial' THEN 'partial' "
                "WHEN status='replied' AND ? IN ('failed','needs_review') THEN 'partial' "
                "WHEN status IN ('failed','needs_review') AND ?='replied' THEN 'partial' "
                "ELSE ? END WHERE id=? AND status!='recalled'",
                (status, status, status, message_id),
            )

    def recall(self, thread: str, server_id: str | None, message_id: str | None):
        with self.db:
            if server_id:
                self.db.execute("INSERT OR IGNORE INTO recalls VALUES (?, ?)", (thread, server_id))
            self.db.execute(
                "UPDATE messages SET status='recalled' WHERE thread=? AND "
                "((? IS NOT NULL AND server_id=?) OR (? IS NULL AND id=?))",
                (thread, server_id, server_id, server_id, message_id),
            )
            if message_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'recalled')",
                    (message_id, thread, server_id),
                )

    def begin_send(self, key: str, message_id: str | None, thread: str):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='queued' WHERE id=? AND status='no_reply'",
                (message_id,),
            )
            self.db.execute(
                "INSERT INTO sends VALUES (?, ?, ?, 'submitting', NULL)", (key, message_id, thread)
            )

    def finish_send(self, key: str, state: str, send_id: str | None):
        with self.db:
            self.db.execute(
                "UPDATE sends SET state=?, send_id=COALESCE(?, send_id) WHERE key=?",
                (state, send_id, key),
            )

    def record_send_id(self, key: str, send_id: str):
        """Retain an asynchronous media job before polling or process shutdown."""
        with self.db:
            self.db.execute("UPDATE sends SET send_id=? WHERE key=?", (send_id, key))

    def send_record(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sends WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def record_voice_chunks(self, key: str, chunks: list):
        """Journal Native's per-note receipts without retaining audio or transcripts."""
        with self.db:
            count = self.db.execute(
                "SELECT COUNT(*) FROM send_chunks WHERE key=?", (key,)
            ).fetchone()[0]
            if count > len(chunks):
                raise StateIdentityError(
                    "Native voice receipts disappeared; inspect the retained job"
                )
            for chunk in chunks:
                existing = self.db.execute(
                    "SELECT * FROM send_chunks WHERE key=? AND part=?", (key, chunk["index"])
                ).fetchone()
                if existing and (
                    existing["duration_ms"] != chunk["durationMs"]
                    or (existing["state"] == "accepted" and chunk["state"] != "accepted")
                    or (
                        existing["server_id"] is not None
                        and existing["server_id"] != chunk.get("serverMessageId")
                    )
                ):
                    raise StateIdentityError(
                        "Native voice receipt changed; inspect the retained job"
                    )
                self.db.execute(
                    "INSERT INTO send_chunks VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key, part) DO UPDATE SET state=excluded.state, "
                    "server_id=COALESCE(excluded.server_id, send_chunks.server_id)",
                    (
                        key,
                        chunk["index"],
                        chunk["state"],
                        chunk["durationMs"],
                        chunk.get("serverMessageId"),
                    ),
                )

    def voice_chunks(self, key: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM send_chunks WHERE key=? ORDER BY part", (key,)
            )
        ]

    def close(self):
        self.db.close()
