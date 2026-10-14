"""Видеоинструкции, которые ИИ и FAQ прикладывают к ответам.

Файлы лежат в media/ (не в git, на сервер кладутся отдельно). Каждый файл
загружается в Telegram один раз; полученный file_id кэшируется в ai.db с
ключом по размеру и времени изменения — заменили файл, кэш сам устарел.
"""
from enum import Enum
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile
from loguru import logger

from app import ai_context
from app.settings import BASE_DIR, SUPPORT_CHAT_ID

MEDIA_DIR = BASE_DIR / "media"


class Video(str, Enum):
    update_subscription = "update_subscription"
    cancel_subscription = "cancel_subscription"
    delete_device = "delete_device"


VIDEO_CAPTIONS = {
    Video.update_subscription: "Как обновить подписку в приложении",
    Video.cancel_subscription: "Как отменить автопродление в боте",
    Video.delete_device: "Как удалить устройство в боте",
}


def _cache_key(video: Video) -> Optional[str]:
    path = MEDIA_DIR / f"{video.value}.mp4"
    if not path.exists():
        return None
    stat = path.stat()
    return f"video:{video.value}:{stat.st_size}:{int(stat.st_mtime)}"


async def send_video_to_topic(bot: Bot, topic_id: int, video: Video) -> Optional[int]:
    """Отправить видео в топик (из кэша file_id или загрузкой файла). Возвращает
    message_id в группе — его потом копируем клиенту. None, если файла нет."""
    key = _cache_key(video)
    if key is None:
        logger.error(f"Video file for {video.value} is missing in {MEDIA_DIR}")
        return None
    caption = VIDEO_CAPTIONS[video]

    file_id = await ai_context.get_setting(key)
    if file_id:
        try:
            sent = await bot.send_video(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id, video=file_id, caption=caption)
            return sent.message_id
        except TelegramBadRequest:
            logger.warning(f"Cached file_id for {video.value} rejected, re-uploading")

    sent = await bot.send_video(
        chat_id=SUPPORT_CHAT_ID,
        message_thread_id=topic_id,
        video=FSInputFile(MEDIA_DIR / f"{video.value}.mp4"),
        caption=caption,
        supports_streaming=True,
    )
    await ai_context.set_setting(key, sent.video.file_id)
    return sent.message_id


async def send_video_to_user(bot: Bot, chat_id: int, video: Video) -> None:
    """Видео напрямую клиенту (для FAQ, без топика): по кэшу file_id, иначе загрузкой."""
    key = _cache_key(video)
    if key is None:
        return
    file_id = await ai_context.get_setting(key)
    source = file_id or FSInputFile(MEDIA_DIR / f"{video.value}.mp4")
    sent = await bot.send_video(chat_id=chat_id, video=source, caption=VIDEO_CAPTIONS[video], supports_streaming=True)
    if not file_id:
        await ai_context.set_setting(key, sent.video.file_id)
