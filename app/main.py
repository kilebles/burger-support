import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from loguru import logger

from app.settings import TOKEN
from app.handlers import router, set_commands
from app.middlewares import AlbumMiddleware, DeduplicationMiddleware
from app.logging_config import setup_logging


async def main():
    setup_logging()
    logger.info("Starting support bot")

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode='HTML')
    )

    dp = Dispatcher()
    dp.update.middleware(DeduplicationMiddleware())
    dp.message.middleware(AlbumMiddleware())
    dp.include_router(router)

    await set_commands(bot)
    logger.info("Bot commands set, starting polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by KeyboardInterrupt")
