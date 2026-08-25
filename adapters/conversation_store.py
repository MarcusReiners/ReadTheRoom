import sqlite3
import uuid
from datetime import datetime

_TITLE_MAX_LEN = 40


def _now() -> str:
    return datetime.now().isoformat()


def _truncate_title(text: str) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) <= _TITLE_MAX_LEN:
        return text
    return text[:_TITLE_MAX_LEN].rstrip() + "…"


class ConversationStore:
    """SQLite-backed store for chat conversations. Opens/closes a connection
    per call rather than sharing one - it's touched from both the turn-
    processing thread and the chat bridge's FastAPI threadpool.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

    def list_conversations(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_conversation(self) -> str:
        conversation_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, "Neuer Chat", now, now),
            )
        return conversation_id

    def get_history(self, conversation_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def add_user_message(self, conversation_id: str, text: str) -> None:
        self._add_message(conversation_id, "user", text)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND role = 'user'",
                (conversation_id,),
            ).fetchone()
            if row["n"] == 1:
                conn.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (_truncate_title(text), conversation_id),
                )

    def replace_or_add_user_message(self, conversation_id: str, text: str) -> bool:
        """If the conversation's last message is still an unanswered user
        message - no assistant reply has followed it yet, e.g. a voice-
        triggered turn a chat-typed message just interrupted, or simply two
        user messages sent back to back before the assistant replied to the
        first - overwrites it in place instead of appending a duplicate.
        Returns True if it replaced, False if it added a new user message as
        usual (the normal case: the last message was the assistant's)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, role FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()

        if row is None or row["role"] != "user":
            self.add_user_message(conversation_id, text)
            return False

        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE messages SET content = ?, created_at = ? WHERE id = ?", (text, now, row["id"]),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            first_user = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if first_user is not None and first_user["id"] == row["id"]:
                # The replaced message was also the title-setting first one.
                conn.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?", (_truncate_title(text), conversation_id),
                )
        return True

    def add_assistant_message(self, conversation_id: str, text: str) -> None:
        self._add_message(conversation_id, "assistant", text)

    def _add_message(self, conversation_id: str, role: str, content: str) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, now),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def get_active_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'active_conversation_id'").fetchone()
        if row is None:
            return None
        conversation_id = row["value"]
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return conversation_id if exists else None

    def set_active_id(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('active_conversation_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (conversation_id,),
            )
