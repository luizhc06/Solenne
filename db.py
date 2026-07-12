import os
import time
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from config import DB_PATH

log = logging.getLogger("hermes-bot")

NEWS_DEDUP_DAYS = 2

BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "backups")
BACKUP_RETENTION_DAYS = 7


def db_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=10)


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                author_name TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat_history(channel_id, id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT,
                summary TEXT DEFAULT '',
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_news (
                link TEXT PRIMARY KEY,
                posted_at TEXT NOT NULL
            )
            """
        )


def save_message(channel_id: int, role: str, author_name: str | None, content: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO chat_history (channel_id, role, author_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, role, author_name, content, datetime.now(timezone.utc).isoformat()),
        )


def load_recent_history(channel_id: int, limit: int = 20) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT role, author_name, content FROM chat_history "
            "WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
    rows.reverse()
    messages = []
    for role, author_name, content in rows:
        text = f"{author_name}: {content}" if role == "user" and author_name else content
        messages.append({"role": role, "content": text})
    return messages


def get_user_summary(user_id: int) -> str:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT summary FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row and row[0] else ""


def save_user_summary(user_id: int, display_name: str, summary: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO user_profiles (user_id, display_name, summary, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "display_name = excluded.display_name, "
            "summary = excluded.summary, "
            "updated_at = excluded.updated_at",
            (user_id, display_name, summary, datetime.now(timezone.utc).isoformat()),
        )


def filter_unposted_links(links: list[str]) -> set[str]:
    """Retorna o subconjunto de links que AINDA NAO foi postado recentemente."""
    if not links:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWS_DEDUP_DAYS)).isoformat()
    placeholders = ",".join("?" for _ in links)
    with db_conn() as conn:
        rows = conn.execute(
            f"SELECT link FROM posted_news WHERE link IN ({placeholders}) AND posted_at >= ?",
            (*links, cutoff),
        ).fetchall()
    already_posted = {r[0] for r in rows}
    return set(links) - already_posted


def mark_news_posted(links: list[str]):
    if not links:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.executemany(
            "INSERT INTO posted_news (link, posted_at) VALUES (?, ?) "
            "ON CONFLICT(link) DO UPDATE SET posted_at = excluded.posted_at",
            [(link, now) for link in links],
        )
        # Limpa entradas antigas pra tabela nao crescer pra sempre.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWS_DEDUP_DAYS * 5)).isoformat()
        conn.execute("DELETE FROM posted_news WHERE posted_at < ?", (cutoff,))


def backup_database_sync():
    if not os.path.exists(DB_PATH):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest_path = os.path.join(BACKUP_DIR, f"solenne-{ts}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    # Limpa backups antigos pra nao encher o disco aos poucos.
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    for fname in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, fname)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            log.exception("Erro ao limpar backup antigo %s", fname)

    log.info("Backup do banco criado: %s", dest_path)
