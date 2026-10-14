import asyncio
import logging
import re
import secrets
import string
import time
from datetime import datetime
from html import escape

from aiogram import BaseMiddleware, F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
	CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
	KeyboardButton, Message, ReplyKeyboardMarkup,
	FSInputFile
)

from config import (
	bot, db, ADMIN_IDS, LINE_CHAR, LOG_CHAT_ID, PROMO_LIMIT,
	PROMO_MAX_FAILS, PROMO_LOCK_SEC, FLOOD_DELAY
)


class Forms(StatesGroup):
	# игрок
	enter_promo = State()
	request_amount = State()
	request_nickname = State()
	request_confirm = State()
	dispute_reason = State()
	# админ
	create_promo_code = State()
	reject_reason = State()
	dispute_deny_reason = State()
	find_user = State()
	broadcast_text = State()
	broadcast_confirm = State()


# фильтр "только админ": для сообщений, чтобы не-админы проваливались дальше (в FSM)
ADMIN = F.from_user.id.in_(ADMIN_IDS)

BANNED: set = set()
bg_tasks: set = set()

DISPUTE_TEXT = {
	"open": "⚖️ спор открыт",
	"accepted": "⚖️ спор принят",
	"denied": "⚖️ спор отклонён",
}


#==========ХЕЛПЕРЫ=========#
DASH_EM = 0.9  # ширина тире в "em": линия длиннее текста — увеличь, короче — уменьши


def _text_width(s: str) -> float:
	# грубая оценка ширины текста в Telegram (пропорциональный шрифт)
	w = 0.0
	for c in s:
		o = ord(c)
		if o in (0xFE0F, 0x200D):
			continue
		if o >= 0x2190:
			w += 1.3
		elif c == " ":
			w += 0.28
		elif c in ".,:;!·'|":
			w += 0.3
		elif c.isdigit():
			w += 0.56
		elif c.isupper():
			w += 0.7
		else:
			w += 0.55
	return w


def head(title: str) -> str:
	# заголовок с линиями по ширине самого заголовка
	plain = re.sub(r"<[^>]+>", "", title)
	n = max(6, round(_text_width(plain) / DASH_EM))
	line = LINE_CHAR * n
	return f"{line}\n{title}\n{line}"


def is_admin(user_id: int) -> bool:
	return user_id in ADMIN_IDS


def fmt(n: int) -> str:
	return f"{n:,}".replace(",", " ")


def short_dt(iso) -> str:
	try:
		return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
	except Exception:
		return "—"


def user_link(row) -> str:
	name = escape(row["full_name"] or "—")
	username = f"@{escape(row['username'])}" if row["username"] else "нет"
	return f"{name} ({username})"


def req_status_text(r) -> str:
	if r["status"] == "pending":
		return "⏳ на рассмотрении"
	if r["status"] == "approved":
		return "🎮 выдано" if r["delivered_at"] else "✅ одобрена, ждёт выдачи"
	if r["decided_by"] == r["telegram_id"]:
		return "🚫 отменена"
	return "❌ отклонена"


def req_can_dispute(r) -> bool:
	if r["status"] == "approved":
		return True
	return r["status"] == "rejected" and r["decided_by"] != r["telegram_id"]


async def require_user(message: Message) -> bool:
	if not await db.get_user(message.from_user.id):
		await message.answer("Сначала нажми /start")
		return False
	return True

_photo_ids: dict = {}  # key -> file_id


async def answer_photo_cached(message: Message, key: str, path: str, caption: str, reply_markup=None):
	photo = _photo_ids.get(key)
	try:
		sent = await message.answer_photo(
			photo or FSInputFile(path), caption=caption, reply_markup=reply_markup
		)
		if not photo and sent and sent.photo:
			_photo_ids[key] = sent.photo[-1].file_id
	except Exception:
		_photo_ids.pop(key, None)
		logging.exception("Не удалось отправить картинку %s", key)
		await message.answer(caption, reply_markup=reply_markup)
	
async def safe_edit(callback: CallbackQuery, text: str, markup=None):
	try:
		await callback.message.edit_text(text, reply_markup=markup)
	except Exception:
		logging.warning("edit_text не удался")


async def safe_markup(callback: CallbackQuery, markup=None):
	try:
		await callback.message.edit_reply_markup(reply_markup=markup)
	except Exception:
		logging.warning("edit_reply_markup не удался")


async def notify_user(user_id: int, text: str):
	try:
		await bot.send_message(user_id, text)
	except Exception:
		logging.warning("Не удалось написать игроку %s", user_id)


async def notify_admins(text: str, markup=None):
	for admin_id in ADMIN_IDS:
		try:
			await bot.send_message(admin_id, text, reply_markup=markup)
		except Exception:
			logging.exception("Не удалось написать админу %s", admin_id)


async def log_action(actor_id: int, action: str, details: str = ""):
	try:
		await db.add_log(actor_id, action, details)
	except Exception:
		logging.exception("Не удалось записать лог")
	if LOG_CHAT_ID:
		try:
			await bot.send_message(
				LOG_CHAT_ID, f"📝 <b>{escape(action)}</b> · <code>{actor_id}</code>\n{escape(details)}"
			)
		except Exception:
			logging.warning("LOG_CHAT_ID недоступен")


#==========КЛАВИАТУРЫ=========#
def main_menu(user_id: int) -> ReplyKeyboardMarkup:
	rows = [
		[KeyboardButton(text="🎟 Ввести промокод"), KeyboardButton(text="💰 Заявка на голду")],
		[KeyboardButton(text="📜 Мои заявки"), KeyboardButton(text="🎁 История промо")],
		[KeyboardButton(text="👤 Профиль")],
	]
	if is_admin(user_id):
		rows.append([KeyboardButton(text="🛡 Админ-панель")])
	return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu() -> InlineKeyboardMarkup:
	def b(text, data):
		return InlineKeyboardButton(text=text, callback_data=data)
	return InlineKeyboardMarkup(inline_keyboard=[
		[b("📊 Мониторинг", "admin:stats"), b("📨 Заявки", "admin:requests")],
		[b("🎮 К выдаче", "admin:delivery"), b("⚖️ Споры", "admin:disputes")],
		[b("📋 Промокоды", "admin:promos"), b("➕ Промокод", "admin:create_promo")],
		[b("🔍 Игрок", "admin:find_user"), b("📣 Рассылка", "admin:broadcast")],
		[b("📜 Лог", "admin:log"), b("💾 Бэкап", "admin:backup")],
	])


def request_keyboard(request_id: int) -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[
			InlineKeyboardButton(text="✅ Одобрить", callback_data=f"request:approve:{request_id}"),
			InlineKeyboardButton(text="❌ Отклонить", callback_data=f"request:reject:{request_id}"),
		]
	])


def deliver_keyboard(request_id: int) -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="🎮 Выдано в игре", callback_data=f"request:deliver:{request_id}")]
	])


def dispute_keyboard(dispute_id: int, req_status: str) -> InlineKeyboardMarkup:
	accept_text = "💰 Вернуть голду" if req_status == "approved" else "🔄 Пересмотреть заявку"
	return InlineKeyboardMarkup(inline_keyboard=[
		[
			InlineKeyboardButton(text=accept_text, callback_data=f"disp:accept:{dispute_id}"),
			InlineKeyboardButton(text="✖️ Отклонить спор", callback_data=f"disp:deny:{dispute_id}"),
		]
	])


#==========КАРТОЧКИ=========#
def request_card(req) -> str:
	return (
		head(f"📨 <b>Заявка №{req['id']}</b>") + f"\n\n"
		f"<blockquote>👤 {user_link(req)}\n"
		f"🆔 <code>{req['telegram_id']}</code>\n"
		f"🎮 Ник: <code>{escape(req['nickname'])}</code>\n"
		f"💰 Сумма: <code>{req['amount']}</code> голды\n"
		f"🕒 {short_dt(req['created_at'])}</blockquote>"
	)


def dispute_card(d) -> str:
	return (
		head(f"⚖️ <b>Спор №{d['id']}</b>") + f"\n\n"
		f"<blockquote>👤 {user_link(d)}\n"
		f"🆔 <code>{d['telegram_id']}</code>\n"
		f"📨 Заявка №{d['request_id']} · <code>{d['amount']}</code> голды\n"
		f"🎮 Ник: <code>{escape(d['nickname'])}</code>\n"
		f"📌 Статус заявки: {escape(dispute_req_status(d))}</blockquote>\n\n"
		f"<blockquote>💬 {escape(d['reason'])}</blockquote>"
	)


def dispute_req_status(d) -> str:
	if d["req_status"] == "approved":
		return "выдано" if d["delivered_at"] else "одобрена, ждёт выдачи"
	if d["req_status"] == "rejected":
		return "отклонена" + (f" ({d['reject_reason']})" if d["reject_reason"] else "")
	return "в ожидании"


#==========ПРОМОКОДЫ: ПЕРЕБОР=========#
_promo_fails: dict = {}  # user_id -> [счётчик, до_какого_времени]


def promo_lock_left(user_id: int) -> int:
	rec = _promo_fails.get(user_id)
	if not rec or rec[1] <= 0:
		return 0
	left = int(rec[1] - time.monotonic())
	if left <= 0:
		_promo_fails.pop(user_id, None)
		return 0
	return left


def promo_fail(user_id: int):
	rec = _promo_fails.setdefault(user_id, [0, 0.0])
	rec[0] += 1
	if rec[0] >= PROMO_MAX_FAILS:
		rec[0] = 0
		rec[1] = time.monotonic() + PROMO_LOCK_SEC


def promo_ok(user_id: int):
	_promo_fails.pop(user_id, None)


#==========СОЗДАНИЕ ПРОМОКОДА=========#
async def do_create_promo(message: Message, code_raw, amount_raw: str) -> bool:
	if not amount_raw.isdigit() or not 0 < int(amount_raw) <= 1_000_000_000:
		await message.answer("❌ Сумма — целое число больше 0.")
		return False
	amount = int(amount_raw)
	admin_id = message.from_user.id
	code = None
	if code_raw is None:
		# авто-код
		for _ in range(5):
			candidate = "LORRY_" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
			if await db.create_promo(candidate, amount, PROMO_LIMIT, admin_id):
				code = candidate
				break
		if not code:
			await message.answer("❌ Не удалось сгенерировать код, попробуй ещё раз.")
			return False
	else:
		candidate = code_raw.upper()
		if not candidate.startswith("LORRY_") or len(candidate) > 64:
			await message.answer("❌ Код должен начинаться с <code>LORRY_</code> и быть до 64 символов.")
			return False
		if not await db.create_promo(candidate, amount, PROMO_LIMIT, admin_id):
			await message.answer("❌ Такой промокод уже существует.")
			return False
		code = candidate
	await message.answer(
		head(f"✅ <b>Промокод создан</b>") + f"\n\n"
		f"<blockquote>🎟 <code>{escape(code)}</code>\n"
		f"🎁 Награда: <b>{fmt(amount)}</b>\n"
		f"👥 Активаций: <b>{PROMO_LIMIT}</b>\n"
		f"💎 Макс. выдача: <b>{fmt(amount * PROMO_LIMIT)}</b></blockquote>"
	)
	await log_action(admin_id, "promo_create", f"{code} · {amount} × {PROMO_LIMIT}")
	return True


#==========КАРТОЧКА ИГРОКА (АДМИН)=========#
def user_keyboard(telegram_id: int, banned: bool) -> InlineKeyboardMarkup:
	if banned:
		btn = InlineKeyboardButton(text="✅ Разбанить", callback_data=f"user:unban:{telegram_id}")
	else:
		btn = InlineKeyboardButton(text="🚫 Забанить", callback_data=f"user:ban:{telegram_id}")
	return InlineKeyboardMarkup(inline_keyboard=[[btn]])


async def build_user_card(telegram_id: int):
	info = await db.get_user_card(telegram_id)
	if not info:
		return None, None
	u = info["user"]
	rq = info["requests"]
	text = (
		head(f"🔍 <b>Игрок</b>") + f"\n\n"
		f"<blockquote>👤 {user_link(u)}\n"
		f"🆔 <code>{u['telegram_id']}</code>\n"
		f"📅 С {short_dt(u['created_at'])}\n"
		f"🚫 Бан: <b>{'да' if u['banned'] else 'нет'}</b></blockquote>\n\n"
		f"<blockquote>💰 Баланс: <b>{fmt(u['balance'])}</b>\n"
		f"🔒 Резерв: <b>{fmt(u['reserved'])}</b>\n"
		f"🎟 Активаций промо: <b>{info['activations']}</b></blockquote>\n\n"
		f"<blockquote>📨 Заявки: ⏳ {rq.get('pending', 0)} · ✅ {rq.get('approved', 0)} · ❌ {rq.get('rejected', 0)}\n"
		f"⚖️ Открытых споров: <b>{info['open_disputes']}</b></blockquote>\n\n"
		f"Правка: <code>+голда {u['telegram_id']} 1000</code> / <code>-голда {u['telegram_id']} 1000</code>"
	)
	return text, user_keyboard(u["telegram_id"], bool(u["banned"]))


async def send_user_card(message: Message, query: str):
	tg_id = await db.find_user_id(query)
	if tg_id is None:
		await message.answer("❌ Игрок не найден. Укажи ID или @username.")
		return False
	text, kb = await build_user_card(tg_id)
	await message.answer(text, reply_markup=kb)
	return True


#==========МИДЛВАРЬ: БАН + АНТИФЛУД=========#
class Guard(BaseMiddleware):
	def __init__(self):
		self.last: dict = {}

	async def __call__(self, handler, event, data):
		user = getattr(event, "from_user", None)
		if user and user.id not in ADMIN_IDS:
			now = time.monotonic()
			if now - self.last.get(user.id, 0.0) < FLOOD_DELAY:
				if isinstance(event, CallbackQuery):
					await event.answer("⏳ Не так быстро")
				return
			self.last[user.id] = now
			if len(self.last) > 5000:
				self.last = {k: v for k, v in self.last.items() if now - v < 60}
			if user.id in BANNED:
				if isinstance(event, CallbackQuery):
					await event.answer("🚫 Доступ ограничен", show_alert=True)
				else:
					await event.answer("🚫 Доступ ограничен")
				return
		return await handler(event, data)


def spawn(coro):
	task = asyncio.create_task(coro)
	bg_tasks.add(task)
	task.add_done_callback(bg_tasks.discard)
	return task
