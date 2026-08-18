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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # WAL (achado do conselho de agentes, 18/08/2026): no modo padrao (rollback
    # journal), UM escritor trava o arquivo INTEIRO - com 20 conversas simultaneas cada
    # uma gravando historico/perfil, mais o backup diario (que le o banco inteiro numa
    # unica transacao), a chance de "database is locked" sobe com o trafego. WAL deixa
    # leitores nunca bloquearem escritores (e vice-versa); e persistido no proprio
    # arquivo do banco, mas idempotente/barato reafirmar em toda conexao. busy_timeout
    # aqui so reforca o timeout=10 acima (SQLITE_BUSY entra na espera do busy_timeout
    # antes do timeout do driver Python valer).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(delivered, due_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS announced_episodes (
                media_id INTEGER NOT NULL,
                episode INTEGER NOT NULL,
                announced_at TEXT NOT NULL,
                PRIMARY KEY (media_id, episode)
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


def load_user_messages(channel_id: int, limit: int = 50) -> list[dict]:
    """So mensagens de usuarios (sem as respostas da propria Solenne) pro /resumo."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT author_name, content FROM chat_history "
            "WHERE channel_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
    rows.reverse()
    return [{"author": author, "content": content} for author, content in rows]


def get_db_stats() -> dict:
    with db_conn() as conn:
        messages = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
        profiles = conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
    return {"messages": messages, "profiles": profiles}


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


def add_reminder(user_id: int, channel_id: int, text: str, due_at: datetime) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, channel_id, text, due_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                channel_id,
                text,
                due_at.astimezone(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def pop_due_reminders() -> list[dict]:
    """Marca como entregues e devolve os lembretes ja vencidos.

    Marca na mesma transacao da leitura de proposito: se o envio no Discord falhar
    depois, o lembrete se perde - preferivel a um loop que reenvia o mesmo lembrete
    a cada 30s pra sempre porque o canal sumiu.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, channel_id, text FROM reminders "
            "WHERE delivered = 0 AND due_at <= ? ORDER BY due_at",
            (now,),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE reminders SET delivered = 1 WHERE id = ?", [(r[0],) for r in rows]
            )
    return [{"id": r[0], "user_id": r[1], "channel_id": r[2], "text": r[3]} for r in rows]


def list_reminders(user_id: int) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, due_at FROM reminders "
            "WHERE user_id = ? AND delivered = 0 ORDER BY due_at",
            (user_id,),
        ).fetchall()
    return [
        {"id": r[0], "text": r[1], "due_at": datetime.fromisoformat(r[2])} for r in rows
    ]


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    """So apaga se o lembrete for da propria pessoa - o id e sequencial e visivel,
    entao sem esse filtro qualquer um cancelaria lembrete dos outros."""
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ? AND delivered = 0",
            (reminder_id, user_id),
        )
        return cur.rowcount > 0


def has_announced_any_episode() -> bool:
    """Se a tabela esta vazia, o radar de anime nunca rodou - o chamador usa isso pra
    semear o estado atual em silencio em vez de anunciar episodios antigos como novos."""
    with db_conn() as conn:
        return conn.execute("SELECT 1 FROM announced_episodes LIMIT 1").fetchone() is not None


def filter_unannounced_episodes(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Dos pares (media_id, episodio) recebidos, devolve os que ainda nao foram anunciados."""
    if not pairs:
        return []
    with db_conn() as conn:
        rows = conn.execute("SELECT media_id, episode FROM announced_episodes").fetchall()
    already = {(r[0], r[1]) for r in rows}
    return [p for p in pairs if p not in already]


def mark_episodes_announced(pairs: list[tuple[int, int]]):
    if not pairs:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.executemany(
            "INSERT INTO announced_episodes (media_id, episode, announced_at) VALUES (?, ?, ?) "
            "ON CONFLICT(media_id, episode) DO NOTHING",
            [(media_id, episode, now) for media_id, episode in pairs],
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        conn.execute("DELETE FROM announced_episodes WHERE announced_at < ?", (cutoff,))


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
