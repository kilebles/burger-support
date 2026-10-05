"""Персистентное состояние ИИ-ассистента: включён ли он (глобально и по
топику), что уже было сказано в каждом топике и текущий статус сбоя.

У Bot API нет метода прочитать историю сообщений топика, так что эта база —
единственный источник правды о ходе разговора: её обязан пополнять каждый
код-путь, который реально отправляет сообщение клиенту от лица поддержки.

Хранится в SQLite-файле в data/ (рядом с topics.json): одна запись на реплику,
нагрузка — единицы запросов в минуту, отдельный сервер БД не нужен.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import aiosqlite

from app.settings import DATA_DIR

Role = Literal["user", "assistant"]

DB_FILE = DATA_DIR / "ai.db"

# Сколько последних реплик топика отдавать модели: старые обращения того же
# клиента к текущему вопросу обычно не относятся, а токены стоят денег.
_HISTORY_LIMIT = 40

# Пинг админа по одному топику не чаще раза в это окно: клиент с проблемой
# может писать каждые полминуты, и каждая реплика иначе давала бы новый тег.
_ESCALATION_COOLDOWN = timedelta(minutes=10)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    topic_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    escalation_notified_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS messages_topic_idx ON messages (topic_id, id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outage_contacts (
    user_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    topic_id INTEGER NOT NULL
);
"""

_db: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()


async def _conn() -> aiosqlite.Connection:
    global _db
    if _db is None:
        async with _db_lock:
            if _db is None:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(DB_FILE)
                await db.executescript(_SCHEMA)
                await db.commit()
                _db = db
    return _db


async def _get_setting(key: str) -> Optional[str]:
    db = await _conn()
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _set_setting(key: str, value: Optional[str]) -> None:
    db = await _conn()
    if value is None:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    await db.commit()


async def is_ai_enabled(topic_id: int) -> bool:
    """ИИ включён по умолчанию в новом топике, пока явно не остановлен /stop."""
    db = await _conn()
    async with db.execute("SELECT enabled FROM topics WHERE topic_id = ?", (topic_id,)) as cur:
        row = await cur.fetchone()
    return True if row is None else bool(row[0])


async def set_ai_enabled(topic_id: int, enabled: bool) -> None:
    db = await _conn()
    await db.execute(
        "INSERT INTO topics (topic_id, enabled) VALUES (?, ?) "
        "ON CONFLICT (topic_id) DO UPDATE SET enabled = excluded.enabled",
        (topic_id, int(enabled)),
    )
    await db.commit()


async def is_ai_globally_enabled() -> bool:
    """Глобальный рубильник поверх потопикового /stop. По умолчанию ВЫКЛЮЧЕН:
    после чистого деплоя ИИ не должен сам начать отвечать реальным клиентам,
    пока админ явно не включит его командой /resumeai."""
    return await _get_setting("ai_enabled") == "1"


async def set_ai_globally_enabled(enabled: bool) -> None:
    await _set_setting("ai_enabled", "1" if enabled else "0")


async def should_notify_escalation(topic_id: int) -> bool:
    """Пора ли реально тегнуть админа по этому топику? Если да — сразу
    фиксирует момент уведомления (проверка и запись в одном месте)."""
    db = await _conn()
    async with db.execute(
        "SELECT escalation_notified_at FROM topics WHERE topic_id = ?", (topic_id,)
    ) as cur:
        row = await cur.fetchone()
    now = datetime.now(timezone.utc)
    if row and row[0] and now - datetime.fromisoformat(row[0]) < _ESCALATION_COOLDOWN:
        return False
    await db.execute(
        "INSERT INTO topics (topic_id, escalation_notified_at) VALUES (?, ?) "
        "ON CONFLICT (topic_id) DO UPDATE SET escalation_notified_at = excluded.escalation_notified_at",
        (topic_id, now.isoformat()),
    )
    await db.commit()
    return True


async def get_history(topic_id: int) -> list[dict[str, str]]:
    db = await _conn()
    async with db.execute(
        "SELECT role, content FROM ("
        "  SELECT id, role, content FROM messages WHERE topic_id = ? ORDER BY id DESC LIMIT ?"
        ") ORDER BY id",
        (topic_id, _HISTORY_LIMIT),
    ) as cur:
        rows = await cur.fetchall()
    return [{"role": role, "content": content} for role, content in rows]


async def append_turn(topic_id: int, role: Role, content: str) -> None:
    """Добавить реплику в историю топика. Пустые реплики не пишем."""
    if not content:
        return
    db = await _conn()
    await db.execute(
        "INSERT INTO messages (topic_id, role, content) VALUES (?, ?, ?)", (topic_id, role, content)
    )
    await db.commit()


async def forget(topic_id: int) -> None:
    """Стереть состояние ИИ для топика, например при удалении топика через /delete."""
    db = await _conn()
    await db.execute("DELETE FROM messages WHERE topic_id = ?", (topic_id,))
    await db.execute("DELETE FROM topics WHERE topic_id = ?", (topic_id,))
    await db.commit()


async def get_outage() -> Optional[str]:
    """Описание идущего массового сбоя (/outage) или None, если сбоя нет."""
    return await _get_setting("outage")


async def set_outage(description: Optional[str]) -> None:
    await _set_setting("outage", description)


async def add_outage_contact(user_id: int, label: str, topic_id: int) -> None:
    """Запомнить клиента, который писал во время сбоя: после починки ему
    нужно написать и начислить компенсацию."""
    db = await _conn()
    await db.execute(
        "INSERT INTO outage_contacts (user_id, label, topic_id) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id) DO UPDATE SET label = excluded.label, topic_id = excluded.topic_id",
        (user_id, label, topic_id),
    )
    await db.commit()


async def pop_outage_contacts() -> list[tuple[int, str, int]]:
    """Забрать и очистить список клиентов, писавших во время сбоя."""
    db = await _conn()
    async with db.execute("SELECT user_id, label, topic_id FROM outage_contacts ORDER BY rowid") as cur:
        rows = await cur.fetchall()
    await db.execute("DELETE FROM outage_contacts")
    await db.commit()
    return [tuple(row) for row in rows]
