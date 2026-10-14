import asyncio
import logging
from html import escape

from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import bot, router, db, ADMIN_IDS, DB_PATH, PROMO_LIMIT
from utils import (
	Forms, ADMIN, BANNED, head, is_admin, fmt, short_dt, safe_markup, notify_user, notify_admins,
	log_action, request_card, request_keyboard, deliver_keyboard, dispute_card, dispute_keyboard,
	do_create_promo, send_user_card, build_user_card, spawn
)


async def deny_access(callback: CallbackQuery) -> bool:
	if is_admin(callback.from_user.id):
		return False
	await callback.answer("Нет доступа", show_alert=True)
	return True


def _parse(data: str, idx: int = 2):
	try:
		return int(data.split(":")[idx])
	except (ValueError, IndexError):
		return None


#==========АДМИН-ПАНЕЛЬ=========#
@router.message(F.text == "🛡 Админ-панель", ADMIN)
async def admin_panel(message: Message, state: FSMContext):
	from utils import admin_menu
	await state.clear()
	await message.answer(head(f"🛡 <b>Админ-панель</b>"), reply_markup=admin_menu())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
	if await deny_access(callback):
		return
	s = await db.get_stats()
	await callback.message.answer(
		head(f"📊 <b>Мониторинг</b>") + f"\n\n"
		f"<blockquote>👥 Игроков: <b>{s['users']}</b>\n"
		f"🎟 Промокодов: <b>{s['promos']}</b> (активаций: {s['uses']})\n"
		f"🎁 Начислено промо: <b>{fmt(s['promo_paid'])}</b>\n"
		f"🔒 В резерве: <b>{fmt(s['reserved'])}</b>\n"
		f"✅ Одобрено заявок на: <b>{fmt(s['approved_sum'])}</b></blockquote>\n\n"
		f"<blockquote>📨 Ждут решения: <b>{s['pending']}</b>\n"
		f"🎮 Ждут выдачи: <b>{s['undelivered']}</b>\n"
		f"⚖️ Открытых споров: <b>{s['open_disputes']}</b></blockquote>\n\n"
		f"<blockquote>📅 Сегодня: +{s['today_users']} игроков · {s['today_uses']} активаций · {s['today_requests']} заявок\n"
		f"🗓 7 дней: +{s['week_users']} игроков · {s['week_uses']} активаций · {s['week_requests']} заявок</blockquote>"
	)
	await callback.answer()


#==========ЗАЯВКИ=========#
@router.callback_query(F.data == "admin:requests")
async def list_requests(callback: CallbackQuery):
	if await deny_access(callback):
		return
	rows = await db.list_pending_requests()
	if not rows:
		await callback.message.answer("📭 Заявок в ожидании нет.")
	for req in rows[:20]:
		await callback.message.answer(request_card(req), reply_markup=request_keyboard(req["id"]))
	await callback.answer()


@router.callback_query(F.data == "admin:delivery")
async def list_delivery(callback: CallbackQuery):
	if await deny_access(callback):
		return
	rows = await db.list_undelivered_requests()
	if not rows:
		await callback.message.answer("📭 Всё выдано, ждущих выдачи нет.")
	for req in rows[:20]:
		await callback.message.answer(request_card(req), reply_markup=deliver_keyboard(req["id"]))
	await callback.answer()


@router.callback_query(F.data.startswith("request:"))
async def process_request(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	try:
		action = callback.data.split(":")[1]
	except IndexError:
		action = ""
	rid = _parse(callback.data)
	if rid is None:
		await callback.answer("Некорректная заявка", show_alert=True)
		return
	admin_id = callback.from_user.id

	if action == "approve":
		res = await db.approve_request(rid, admin_id)
		if not res["ok"]:
			await callback.answer(res["message"], show_alert=True)
			return
		await safe_markup(callback, deliver_keyboard(rid))
		await callback.message.answer(
			f"✅ <b>Заявка №{rid} одобрена</b>\n"
			f"<blockquote>Списано: <b>{fmt(res['amount'])}</b> голды\n"
			f"🎮 Ник: <code>{escape(res['nickname'])}</code>\n"
			f"💰 Сумма: <code>{res['amount']}</code>\n\n"
			f"Выдай голду в игре и нажми «🎮 Выдано в игре».</blockquote>"
		)
		await notify_user(
			res["telegram_id"],
			f"✅ <b>Заявка №{rid} одобрена</b>\n"
			f"<blockquote>Голда будет выдана на ник <b>{escape(res['nickname'])}</b></blockquote>",
		)
		await log_action(admin_id, "request_approve", f"№{rid} · {res['amount']} · {res['nickname']}")
		await callback.answer("Одобрено")

	elif action == "reject":
		await state.set_state(Forms.reject_reason)
		await state.update_data(
			request_id=rid,
			card_chat=callback.message.chat.id,
			card_msg=callback.message.message_id,
		)
		await callback.message.answer(
			f"❌ <b>Отклонение заявки №{rid}</b>\n\n"
			"Напиши причину отказа (игрок её увидит).\n"
			"<code>-</code> — без причины, <code>отмена</code> — не отклонять."
		)
		await callback.answer()

	elif action == "deliver":
		res = await db.mark_delivered(rid, admin_id)
		if not res["ok"]:
			await callback.answer(res["message"], show_alert=True)
			return
		await safe_markup(callback, None)
		await callback.message.answer(f"🎮 <b>Заявка №{rid} выдана в игре</b>")
		await notify_user(
			res["telegram_id"],
			f"🎮 <b>Голда выдана!</b>\n"
			f"<blockquote>Заявка №{rid} · {fmt(res['amount'])} голды\n"
			f"Ник: <b>{escape(res['nickname'])}</b></blockquote>\n\n"
			"Если голда не пришла — оспорь заявку в 📜 Мои заявки.",
		)
		await log_action(admin_id, "request_deliver", f"№{rid} · {res['amount']} · {res['nickname']}")
		await callback.answer("Отмечено выданной")

	else:
		await callback.answer("Неизвестное действие", show_alert=True)


#==========СПОРЫ=========#
@router.callback_query(F.data == "admin:disputes")
async def list_disputes(callback: CallbackQuery):
	if await deny_access(callback):
		return
	rows = await db.list_open_disputes()
	if not rows:
		await callback.message.answer("📭 Открытых споров нет.")
	for d in rows[:20]:
		await callback.message.answer(dispute_card(d), reply_markup=dispute_keyboard(d["id"], d["req_status"]))
	await callback.answer()


@router.callback_query(F.data.startswith("disp:"))
async def process_dispute(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	try:
		action = callback.data.split(":")[1]
	except IndexError:
		action = ""
	did = _parse(callback.data)
	if did is None:
		await callback.answer("Некорректный спор", show_alert=True)
		return
	admin_id = callback.from_user.id

	if action == "accept":
		res = await db.accept_dispute(did, admin_id)
		if not res["ok"]:
			await callback.answer(res["message"], show_alert=True)
			return
		await safe_markup(callback, None)
		rid = res["request_id"]
		if res["mode"] == "refund":
			await callback.message.answer(
				f"💰 <b>Спор №{did} принят</b>\n"
				f"<blockquote>Игроку возвращено <b>{fmt(res['amount'])}</b> голды на баланс</blockquote>"
			)
			await notify_user(
				res["telegram_id"],
				f"⚖️ <b>Спор по заявке №{rid} принят</b>\n"
				f"<blockquote>💰 {fmt(res['amount'])} голды возвращено на твой баланс</blockquote>",
			)
		else:
			await callback.message.answer(
				f"🔄 <b>Спор №{did} принят</b>\nЗаявка №{rid} снова в работе, сумма в резерве."
			)
			req = await db.get_request(rid)
			await callback.message.answer(request_card(req), reply_markup=request_keyboard(rid))
			await notify_user(
				res["telegram_id"],
				f"⚖️ <b>Спор по заявке №{rid} принят</b>\n"
				"<blockquote>🔄 Заявка возвращена на рассмотрение</blockquote>",
			)
		await log_action(admin_id, "dispute_accept", f"спор №{did} · заявка №{rid} · {res['mode']}")
		await callback.answer("Принято")

	elif action == "deny":
		await state.set_state(Forms.dispute_deny_reason)
		await state.update_data(
			dispute_id=did,
			card_chat=callback.message.chat.id,
			card_msg=callback.message.message_id,
		)
		await callback.message.answer(
			f"✖️ <b>Отклонение спора №{did}</b>\n\n"
			"Напиши ответ игроку.\n"
			"<code>-</code> — без пояснения, <code>отмена</code> — не отклонять."
		)
		await callback.answer()

	else:
		await callback.answer("Неизвестное действие", show_alert=True)


#==========ПРОМОКОДЫ=========#
async def build_promos():
	rows = await db.list_promos(15)
	if not rows:
		return "📭 Промокодов пока нет.", None
	lines = []
	buttons = []
	for p in rows:
		mark = "🟢" if p["active"] else "🔴"
		lines.append(
			f"{mark} <code>{escape(p['code'])}</code> · {p['uses']}/{p['max_uses']} · {fmt(p['amount'])}"
		)
		label = ("🚫 Выкл " if p["active"] else "✅ Вкл ") + p["code"]
		buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"promo:toggle:{p['rid']}")])
	text = head(f"📋 <b>Промокоды</b>") + f"\n\n<blockquote>" + "\n".join(lines) + "</blockquote>"
	return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin:promos")
async def list_promos(callback: CallbackQuery):
	if await deny_access(callback):
		return
	text, kb = await build_promos()
	await callback.message.answer(text, reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data.startswith("promo:toggle:"))
async def toggle_promo(callback: CallbackQuery):
	if await deny_access(callback):
		return
	rid = _parse(callback.data)
	promo = await db.toggle_promo(rid) if rid is not None else None
	if not promo:
		await callback.answer("Промокод не найден", show_alert=True)
		return
	await log_action(
		callback.from_user.id, "promo_toggle",
		f"{promo['code']} → {'вкл' if promo['active'] else 'выкл'}",
	)
	text, kb = await build_promos()
	try:
		await callback.message.edit_text(text, reply_markup=kb)
	except TelegramBadRequest:
		pass
	await callback.answer("Включён" if promo["active"] else "Отключён")


@router.callback_query(F.data == "admin:create_promo")
async def create_promo_start(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	await state.set_state(Forms.create_promo_code)
	await callback.message.answer(
		"➕ Отправь:\n"
		"<code>СУММА</code> — код сгенерируется сам\n"
		"<code>КОД СУММА</code> — свой код\n"
		f"Лимит активаций: <b>{PROMO_LIMIT}</b>"
	)
	await callback.answer()


@router.message(F.text.lower().startswith("+промо"), ADMIN)
async def plus_promo(message: Message):
	parts = message.text.split()
	if len(parts) == 2:
		await do_create_promo(message, None, parts[1])
	elif len(parts) == 3:
		await do_create_promo(message, parts[1], parts[2])
	else:
		await message.answer(
			"Формат:\n<code>+промо 5000</code> — авто-код\n<code>+промо LORRY_КОД 5000</code>"
		)


#==========ИГРОКИ: КАРТОЧКА, БАН, ГОЛДА=========#
@router.callback_query(F.data == "admin:find_user")
async def find_user_start(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	await state.set_state(Forms.find_user)
	await callback.message.answer("🔍 Отправь ID или @username игрока.")
	await callback.answer()


@router.message(Command("user"), ADMIN)
async def user_command(message: Message, command: CommandObject):
	if not command.args:
		await message.answer("Формат: <code>/user ID</code> или <code>/user @ник</code>")
		return
	await send_user_card(message, command.args)


async def _set_ban(admin_id: int, query: str, banned: bool):
	tg_id = await db.find_user_id(query)
	if tg_id is None:
		return None
	if tg_id in ADMIN_IDS:
		return "admin"
	await db.set_banned(tg_id, banned)
	if banned:
		BANNED.add(tg_id)
	else:
		BANNED.discard(tg_id)
	await log_action(admin_id, "ban" if banned else "unban", str(tg_id))
	return tg_id


@router.message(Command("ban"), ADMIN)
async def ban_command(message: Message, command: CommandObject):
	if not command.args:
		await message.answer("Формат: <code>/ban ID</code> или <code>/ban @ник</code>")
		return
	res = await _set_ban(message.from_user.id, command.args, True)
	if res is None:
		await message.answer("❌ Игрок не найден.")
	elif res == "admin":
		await message.answer("❌ Админа банить нельзя.")
	else:
		await message.answer(f"🚫 Игрок <code>{res}</code> забанен.")


@router.message(Command("unban"), ADMIN)
async def unban_command(message: Message, command: CommandObject):
	if not command.args:
		await message.answer("Формат: <code>/unban ID</code> или <code>/unban @ник</code>")
		return
	res = await _set_ban(message.from_user.id, command.args, False)
	if res is None:
		await message.answer("❌ Игрок не найден.")
	else:
		await message.answer(f"✅ Игрок <code>{res}</code> разбанен.")


@router.callback_query(F.data.startswith("user:"))
async def user_ban_buttons(callback: CallbackQuery):
	if await deny_access(callback):
		return
	try:
		action = callback.data.split(":")[1]
	except IndexError:
		action = ""
	tg_id = _parse(callback.data)
	if tg_id is None or action not in ("ban", "unban"):
		await callback.answer("Некорректно", show_alert=True)
		return
	res = await _set_ban(callback.from_user.id, str(tg_id), action == "ban")
	if res is None:
		await callback.answer("Игрок не найден", show_alert=True)
		return
	if res == "admin":
		await callback.answer("Админа банить нельзя", show_alert=True)
		return
	text, kb = await build_user_card(tg_id)
	try:
		await callback.message.edit_text(text, reply_markup=kb)
	except TelegramBadRequest:
		pass
	await callback.answer("Забанен" if action == "ban" else "Разбанен")


@router.message(F.text.regexp(r"(?i)^[+-]голда(\s|$)"), ADMIN)
async def adjust_gold(message: Message):
	sign = 1 if message.text.strip()[0] == "+" else -1
	parts = message.text.split()
	if len(parts) != 3 or not parts[2].isdigit() or not 0 < int(parts[2]) <= 1_000_000_000:
		await message.answer("Формат: <code>+голда ID 1000</code> или <code>-голда @ник 1000</code>")
		return
	tg_id = await db.find_user_id(parts[1])
	if tg_id is None:
		await message.answer("❌ Игрок не найден.")
		return
	amount = int(parts[2])
	res = await db.adjust_balance(tg_id, sign * amount)
	if not res["ok"]:
		await message.answer(f"❌ {res['message']}")
		return
	sign_text = "+" if sign > 0 else "−"
	await message.answer(
		f"✅ <b>Баланс изменён</b>\n"
		f"<blockquote>🆔 <code>{tg_id}</code>\n"
		f"{sign_text}{fmt(amount)} → <b>{fmt(res['balance'])}</b> голды</blockquote>"
	)
	await log_action(message.from_user.id, "gold_adjust", f"{tg_id} · {sign_text}{amount} → {res['balance']}")


#==========ЛОГ, БЭКАП=========#
async def send_log(message: Message):
	rows = await db.list_logs(15)
	if not rows:
		await message.answer("📭 Лог пуст.")
		return
	lines = "\n".join(
		f"{short_dt(r['created_at'])} · <code>{r['actor_id']}</code> · {escape(r['action'])} · {escape(r['details'])}"
		for r in rows
	)
	await message.answer(head(f"📜 <b>Лог действий</b>") + f"\n\n<blockquote>{lines}</blockquote>")


async def send_backup(message: Message):
	await db.checkpoint()
	await message.answer_document(
		FSInputFile(DB_PATH),
		caption="💾 Бэкап БД. Внутри данные игроков — не пересылай никому.",
	)
	await log_action(message.from_user.id, "backup", "")


@router.message(Command("log"), ADMIN)
async def log_command(message: Message):
	await send_log(message)


@router.message(Command("backup"), ADMIN)
async def backup_command(message: Message):
	await send_backup(message)


@router.callback_query(F.data == "admin:log")
async def log_button(callback: CallbackQuery):
	if await deny_access(callback):
		return
	await send_log(callback.message)
	await callback.answer()


@router.callback_query(F.data == "admin:backup")
async def backup_button(callback: CallbackQuery):
	if await deny_access(callback):
		return
	# callback.message.from_user — бот, поэтому шлём напрямую тому, кто нажал
	await db.checkpoint()
	await bot.send_document(
		callback.from_user.id,
		FSInputFile(DB_PATH),
		caption="💾 Бэкап БД. Внутри данные игроков — не пересылай никому.",
	)
	await log_action(callback.from_user.id, "backup", "")
	await callback.answer()


#==========РАССЫЛКА=========#
@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	await state.set_state(Forms.broadcast_text)
	await callback.message.answer(
		"📣 Пришли текст рассылки (HTML-теги можно).\n<code>отмена</code> — выйти."
	)
	await callback.answer()


async def run_broadcast(admin_id: int, text: str):
	ids = await db.get_all_user_ids()
	ok = fail = 0
	for uid in ids:
		try:
			await bot.send_message(uid, text)
			ok += 1
		except TelegramRetryAfter as e:
			await asyncio.sleep(e.retry_after)
			try:
				await bot.send_message(uid, text)
				ok += 1
			except Exception:
				fail += 1
		except Exception:
			fail += 1
		await asyncio.sleep(0.05)
	await log_action(admin_id, "broadcast", f"✅ {ok} · ❌ {fail}")
	try:
		await bot.send_message(admin_id, f"📣 <b>Рассылка завершена</b>\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")
	except Exception:
		logging.warning("Не удалось отчитаться админу о рассылке")


@router.callback_query(Forms.broadcast_confirm, F.data == "bc:yes")
async def broadcast_yes(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	data = await state.get_data()
	await state.clear()
	await safe_markup(callback, None)
	spawn(run_broadcast(callback.from_user.id, data["broadcast_text"]))
	await callback.message.answer("📣 Рассылка запущена. Пришлю отчёт, когда закончится.")
	await callback.answer()


@router.callback_query(Forms.broadcast_confirm, F.data == "bc:no")
async def broadcast_no(callback: CallbackQuery, state: FSMContext):
	if await deny_access(callback):
		return
	await state.clear()
	await safe_markup(callback, None)
	await callback.message.answer("🚫 Рассылка отменена.")
	await callback.answer()


@router.callback_query(F.data.startswith("bc:"))
async def broadcast_stale(callback: CallbackQuery):
	await safe_markup(callback, None)
	await callback.answer("Сессия устарела. Начни рассылку заново.", show_alert=True)
