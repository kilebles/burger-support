import asyncio
import json
from collections import deque
from typing import Any, Awaitable, Callable, Dict

import aiofiles
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from loguru import logger

from app.settings import DATA_DIR

SEEN_UPDATES_FILE = DATA_DIR / "seen_updates.json"
MAX_TRACKED = 500


class DeduplicationMiddleware(BaseMiddleware):
    """Skip Telegram updates already processed.

    aiogram confirms an update's offset to Telegram as soon as it's handed off
    for processing, not after the handler finishes - if the process restarts
    mid-handler (e.g. the watchfiles auto-reload in dev), Telegram will
    redeliver that same update on the next poll. Without this guard it would
    be forwarded twice.
    """

    def __init__(self) -> None:
        self._seen: deque[int] = deque(maxlen=MAX_TRACKED)
        self._seen_set: set[int] = set()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if SEEN_UPDATES_FILE.exists():
            async with aiofiles.open(SEEN_UPDATES_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
            for update_id in (json.loads(raw) if raw else []):
                self._seen.append(update_id)
                self._seen_set.add(update_id)

    async def _persist(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(SEEN_UPDATES_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(list(self._seen)))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any]
    ) -> Any:
        async with self._lock:
            await self._load()

            if event.update_id in self._seen_set:
                logger.warning(f"Skipped duplicate update_id={event.update_id}")
                return None

            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(event.update_id)
            self._seen_set.add(event.update_id)
            await self._persist()

        return await handler(event, data)
