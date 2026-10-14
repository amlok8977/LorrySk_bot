from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import logging
import os

from database import Database

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
logging.basicConfig(level=logging.INFO)

#==========СЕКРЕТЫ (.env)=========#
token_bot = os.getenv("TOKEN")

ADMIN_IDS = {
	int(value.strip())
	for value in os.getenv("ADMIN_IDS", "").split(",")
	if value.strip().isdigit()
}
# чат для лога действий админов (необязательно, 0 = выключено)
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID") or 0)

#==========НАСТРОЙКИ=========#
DB_PATH = os.getenv("DB_PATH") or os.path.join(BASE_DIR, "lorry.sqlite3")
PROMO_LIMIT = 5
MIN_REQUEST = 5000
LINE_CHAR = "—"
IMG_START = os.path.join(BASE_DIR, "images", "start.jpg")
IMG_PROFILE = os.path.join(BASE_DIR, "images", "profile.jpg")
PROMO_MAX_FAILS = 5      # неверных промокодов подряд
PROMO_LOCK_SEC = 600     # блокировка ввода промокодов, сек
FLOOD_DELAY = 0.6        # минимум между действиями игрока, сек

#=======AIOGRAM ОБЪЕКТЫ=======#
bot = Bot(
	token=token_bot,
	default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()
router = Router()
db = Database(DB_PATH)
