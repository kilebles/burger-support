import asyncio

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from loguru import logger

# Не ждём дольше этого: при долгом бане лучше отдать ошибку, чем подвесить хендлер.
_MAX_WAIT_SECONDS = 30
_MAX_ATTEMPTS = 3


class RetryAfterMiddleware(BaseRequestMiddleware):
    """Повторяет запрос к Bot API после флуд-контроля Telegram.

    Группа поддержки упирается в лимит ~20 сообщений в минуту, когда сразу
    несколько клиентов пишут пачками: без повтора CopyMessage падал с
    "Flood control exceeded ... Retry in 5 seconds", сообщение клиента в топик
    не доходило, а клиент получал "Не удалось отправить".
    """

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as error:
                if attempt == _MAX_ATTEMPTS or error.retry_after > _MAX_WAIT_SECONDS:
                    raise
                logger.warning(f"Flood control on {type(method).__name__}, retrying in {error.retry_after}s")
                await asyncio.sleep(error.retry_after + 0.5)
        raise AssertionError("unreachable")
