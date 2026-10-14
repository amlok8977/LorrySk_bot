import asyncio
import logging

from config import bot, dp, router, db
from utils import Guard, BANNED

import handlers_user
import handlers_admin 
import handlers_fsm  

router.message.outer_middleware(Guard())
router.callback_query.outer_middleware(Guard())


async def main():
	await db.initialize()
	BANNED.update(await db.get_banned_ids())
	dp.include_router(router)
	logging.info("Бот запущен")
	try:
		await dp.start_polling(bot)
	finally:
		await db.close()
		await bot.session.close()


if __name__ == "__main__":
	asyncio.run(main())
