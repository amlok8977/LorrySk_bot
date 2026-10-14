from html import escape

from aiogram import F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from utils import answer_photo_cached
from config import router, db, MIN_REQUEST, IMG_START, IMG_PROFILE
from utils import (
	Forms, DISPUTE_TEXT, head, is_admin, fmt, short_dt, main_menu, require_user,
	safe_edit, safe_markup, notify_admins, log_action, request_card, request_keyboard,
	req_status_text, req_can_dispute, promo_lock_left
)


#==========КОМАНДЫ=========#
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
	await state.clear()
	user = message.from_user
	await db.register_user(user.id, user.username or "", user.full_name)
	await answer_photo_cached(
		message, "start", IMG_START,
		"<blockquote>🎟 Промокоды\n"
		"💰 Копи голду и оформляй заявки на вывод</blockquote>\n\n"
		"Выбери действие в меню 👇",
		reply_markup=main_menu(user.id),
	)


@router.message(Command("help"))
async def help_command(message: Message):
	text = (
		head(f"❓ <b>Помощь</b>") + f"\n\n"
		"/start — запуск\n/help — помощь\n\n"
		f"<blockquote>💰 Вывод от <b>{fmt(MIN_REQUEST)}</b> голды\n"
		"📜 Мои заявки — статус, отмена, оспаривание</blockquote>"
	)
	if is_admin(message.from_user.id):
		text += (
			"\n\n🛡 <b>Админ:</b>\n"
			"<code>+промо 5000</code> — авто-код\n"
			"<code>+промо LORRY_КОД 5000</code>\n"
			"<code>/user ID|@ник</code> · <code>/ban</code> · <code>/unban</code>\n"
			"<code>+голда ID 1000</code> · <code>-голда ID 1000</code>\n"
			"<code>/backup</code> · <code>/log</code>"
		)
	await message.answer(text)


#==========МЕНЮ ИГРОКА=========#
@router.message(F.text == "👤 Профиль")
async def profile(message: Message, state: FSMContext):
	await state.clear()
	if not await require_user(message):
		return
	user = await db.get_user(message.from_user.id)
	await answer_photo_cached(
		message, "profile", IMG_PROFILE,
		f"<blockquote>🆔 <code>{message.from_user.id}</code>\n"
		f"💰 Баланс: <b>{fmt(user['balance'])}</b> голды\n"
		f"🔒 В резерве: <b>{fmt(user['reserved'])}</b> голды</blockquote>",
	)


@router.message(F.text == "🎟 Ввести промокод")
async def promo_start(message: Message, state: FSMContext):
	await state.clear()
	if not await require_user(message):
		return
	left = promo_lock_left(message.from_user.id)
	if left:
		await message.answer(f"⏳ Слишком много неверных кодов. Попробуй через {left // 60 + 1} мин.")
		return
	await state.set_state(Forms.enter_promo)
	await message.answer("🎟 Введи промокод.\nНапример: <code>LORRY_ABCD1234</code>")


@router.message(F.text == "🎁 История промо")
async def promo_history(message: Message, state: FSMContext):
	await state.clear()
	if not await require_user(message):
		return
	rows = await db.list_user_activations(message.from_user.id)
	if not rows:
		await message.answer("🎁 Активаций пока нет.")
		return
	lines = "\n".join(
		f"{short_dt(r['activated_at'])} · <code>{escape(r['code'])}</code> · +{fmt(r['amount'])}"
		for r in rows
	)
	await message.answer(head(f"🎁 <b>История промо</b>") + f"\n\n<blockquote>{lines}</blockquote>")


@router.message(F.text == "💰 Заявка на голду")
async def request_start(message: Message, state: FSMContext):
	await state.clear()
	if not await require_user(message):
		return
	active = await db.get_pending_request(message.from_user.id)
	if active:
		await message.answer(
			f"❌ У тебя уже есть активная заявка №{active['id']}.\n"
			"Дождись решения или отмени её в 📜 Мои заявки."
		)
		return
	await state.set_state(Forms.request_amount)
	await message.answer(f"💰 Введи количество голды (минимум <b>{fmt(MIN_REQUEST)}</b>).")


#==========МОИ ЗАЯВКИ=========#
async def build_my_requests(user_id: int):
	rows = await db.list_user_requests(user_id, 10)
	if not rows:
		return "📭 Заявок пока нет.", None
	lines = []
	for r in rows:
		line = f"№{r['id']} · {fmt(r['amount'])} · {req_status_text(r)} · {short_dt(r['created_at'])}"
		if r["dispute_status"]:
			line += f"\n   {DISPUTE_TEXT[r['dispute_status']]}"
		if r["status"] == "rejected" and r["reject_reason"] and r["decided_by"] != r["telegram_id"]:
			line += f"\n   └ {escape(r['reject_reason'])}"
		lines.append(line)
	text = head(f"📜 <b>Мои заявки</b>") + f"\n\n<blockquote>" + "\n".join(lines) + "</blockquote>"
	buttons = []
	for r in rows[:5]:
		if r["status"] == "pending":
			buttons.append([InlineKeyboardButton(text=f"🚫 Отменить №{r['id']}", callback_data=f"my:cancel:{r['id']}")])
		elif req_can_dispute(r) and not r["dispute_status"]:
			buttons.append([InlineKeyboardButton(text=f"⚖️ Оспорить №{r['id']}", callback_data=f"my:dispute:{r['id']}")])
	return text, (InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)


@router.message(F.text == "📜 Мои заявки")
async def my_requests(message: Message, state: FSMContext):
	await state.clear()
	if not await require_user(message):
		return
	text, kb = await build_my_requests(message.from_user.id)
	await message.answer(text, reply_markup=kb)


def _parse_id(data: str):
	try:
		return int(data.split(":")[2])
	except (ValueError, IndexError):
		return None


@router.callback_query(F.data.startswith("my:cancel:"))
async def my_cancel(callback: CallbackQuery):
	rid = _parse_id(callback.data)
	if rid is None:
		await callback.answer("Некорректная заявка", show_alert=True)
		return
	res = await db.cancel_request(rid, callback.from_user.id)
	if not res["ok"]:
		await callback.answer(res["message"], show_alert=True)
		return
	await callback.answer("Заявка отменена, голда снова доступна")
	await log_action(callback.from_user.id, "request_cancel", f"№{rid} · {res['amount']}")
	text, kb = await build_my_requests(callback.from_user.id)
	await safe_edit(callback, text, kb)


@router.callback_query(F.data.startswith("my:dispute:"))
async def my_dispute(callback: CallbackQuery, state: FSMContext):
	rid = _parse_id(callback.data)
	if rid is None:
		await callback.answer("Некорректная заявка", show_alert=True)
		return
	ok, message = await db.can_dispute(callback.from_user.id, rid)
	if not ok:
		await callback.answer(message, show_alert=True)
		return
	await state.set_state(Forms.dispute_reason)
	await state.update_data(dispute_request_id=rid)
	await callback.message.answer(
		f"⚖️ <b>Оспаривание заявки №{rid}</b>\n\n"
		"Опиши проблему одним сообщением (10–500 символов).\n"
		"Передумал — напиши <code>отмена</code>."
	)
	await callback.answer()


#==========ПОДТВЕРЖДЕНИЕ ЗАЯВКИ=========#
@router.callback_query(Forms.request_confirm, F.data == "req:confirm")
async def req_confirm(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	await state.clear()
	await safe_markup(callback, None)
	user_id = callback.from_user.id
	res = await db.create_request(user_id, int(data["amount"]), data["nickname"])
	if not res["ok"]:
		await callback.message.answer(res["message"], reply_markup=main_menu(user_id))
		await callback.answer()
		return
	request_id = res["request_id"]
	await callback.message.answer(
		head(f"✅ <b>Заявка №{request_id} создана</b>") + f"\n\n"
		f"<blockquote>💰 {fmt(res['amount'])} голды\n"
		f"🎮 Ник: <b>{escape(data['nickname'])}</b>\n"
		f"🔒 Сумма в резерве до решения админа</blockquote>\n\n"
		"Отменить можно в 📜 Мои заявки.",
		reply_markup=main_menu(user_id),
	)
	req = await db.get_request(request_id)
	await notify_admins(request_card(req), request_keyboard(request_id))
	await log_action(user_id, "request_create", f"№{request_id} · {res['amount']} · {data['nickname']}")
	await callback.answer()


@router.callback_query(Forms.request_confirm, F.data == "req:cancel")
async def req_cancel_confirm(callback: CallbackQuery, state: FSMContext):
	await state.clear()
	await safe_markup(callback, None)
	await callback.message.answer("🚫 Заявка не создана.", reply_markup=main_menu(callback.from_user.id))
	await callback.answer()


@router.callback_query(F.data.startswith("req:"))
async def req_stale(callback: CallbackQuery):
	# сюда попадаем, если состояние потерялось (например, после перезапуска бота)
	await safe_markup(callback, None)
	await callback.answer("Сессия устарела. Нажми «💰 Заявка на голду» заново.", show_alert=True)
