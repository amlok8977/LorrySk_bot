from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import bot, router, db, MIN_REQUEST
from utils import (
	Forms, head, is_admin, fmt, main_menu, safe_markup, notify_user, notify_admins, log_action,
	dispute_card, dispute_keyboard, do_create_promo, send_user_card,
	promo_lock_left, promo_fail, promo_ok
)

# ВАЖНО: этот модуль импортируется в main.py ПОСЛЕДНИМ.
# Хендлеры состояний должны регистрироваться после кнопок меню,
# иначе кнопка меню в состоянии съедается как обычный ввод.


def _is_cancel(text: str) -> bool:
	return text.strip().lower() in ("отмена", "/cancel")


async def _clear_card(data: dict):
	# убираем кнопки у исходной карточки после решения админа
	try:
		await bot.edit_message_reply_markup(
			chat_id=data["card_chat"], message_id=data["card_msg"], reply_markup=None
		)
	except Exception:
		pass


#==========ИГРОК=========#
@router.message(Forms.enter_promo)
async def redeem_promo(message: Message, state: FSMContext):
	uid = message.from_user.id
	left = promo_lock_left(uid)
	if left:
		await state.clear()
		await message.answer(
			f"⏳ Слишком много неверных кодов. Попробуй через {left // 60 + 1} мин.",
			reply_markup=main_menu(uid),
		)
		return
	code = (message.text or "").strip().upper()
	if not code or len(code) > 64:
		await message.answer("Отправь промокод текстом.")
		return
	result = await db.redeem_promo(uid, code)
	await state.clear()
	if result["ok"]:
		promo_ok(uid)
		await message.answer(
			head(f"✅ <b>Промокод активирован!</b>") + f"\n\n"
			f"<blockquote>🎁 Начислено: <b>{fmt(result['amount'])}</b> голды</blockquote>",
			reply_markup=main_menu(uid),
		)
		await log_action(uid, "promo_redeem", f"{code} · +{result['amount']}")
		return
	if result.get("code") in ("notfound", "inactive"):
		promo_fail(uid)
	await message.answer(result["message"], reply_markup=main_menu(uid))


@router.message(Forms.request_amount)
async def request_amount(message: Message, state: FSMContext):
	raw = (message.text or "").strip()
	if not raw.isdigit() or not MIN_REQUEST <= int(raw) <= 1_000_000_000:
		await message.answer(f"❌ Минимум для вывода — <b>{fmt(MIN_REQUEST)}</b> голды.")
		return
	amount = int(raw)
	user = await db.get_user(message.from_user.id)
	available = user["balance"] - user["reserved"]
	if amount > available:
		await message.answer(f"❌ Недостаточно доступной голды.\nДоступно: <b>{fmt(available)}</b>")
		return
	await state.update_data(amount=amount)
	await state.set_state(Forms.request_nickname)
	await message.answer("🎮 Введи игровой ник, на который оформляется заявка.")


@router.message(Forms.request_nickname)
async def request_nickname(message: Message, state: FSMContext):
	nickname = (message.text or "").strip()
	if not nickname or len(nickname) > 64 or "\n" in nickname:
		await message.answer("Ник должен быть в одну строку, от 1 до 64 символов.")
		return
	data = await state.get_data()
	await state.update_data(nickname=nickname)
	await state.set_state(Forms.request_confirm)
	kb = InlineKeyboardMarkup(inline_keyboard=[[
		InlineKeyboardButton(text="✅ Подтвердить", callback_data="req:confirm"),
		InlineKeyboardButton(text="🚫 Отмена", callback_data="req:cancel"),
	]])
	await message.answer(
		head(f"📝 <b>Проверь заявку</b>") + f"\n\n"
		f"<blockquote>💰 Сумма: <b>{fmt(int(data['amount']))}</b> голды\n"
		f"🎮 Ник: <b>{escape(nickname)}</b></blockquote>\n\n"
		"Ник неверный — отмени и создай заново, исправить после отправки нельзя.",
		reply_markup=kb,
	)


@router.message(Forms.dispute_reason)
async def dispute_reason(message: Message, state: FSMContext):
	text = (message.text or "").strip()
	if _is_cancel(text):
		await state.clear()
		await message.answer("🚫 Оспаривание отменено.", reply_markup=main_menu(message.from_user.id))
		return
	if not 10 <= len(text) <= 500:
		await message.answer("Опиши подробнее: от 10 до 500 символов. Или напиши <code>отмена</code>.")
		return
	data = await state.get_data()
	await state.clear()
	uid = message.from_user.id
	res = await db.create_dispute(uid, int(data["dispute_request_id"]), text)
	if not res["ok"]:
		await message.answer(f"❌ {res['message']}", reply_markup=main_menu(uid))
		return
	dispute_id = res["dispute_id"]
	await message.answer(
		head(f"⚖️ <b>Спор №{dispute_id} отправлен</b>") + f"\n\n"
		"<blockquote>Админ рассмотрит и напишет тебе решение.</blockquote>",
		reply_markup=main_menu(uid),
	)
	d = await db.get_dispute(dispute_id)
	await notify_admins(dispute_card(d), dispute_keyboard(dispute_id, d["req_status"]))
	await log_action(uid, "dispute_create", f"спор №{dispute_id} · заявка №{d['request_id']}")


#==========АДМИН=========#
@router.message(Forms.reject_reason)
async def reject_reason(message: Message, state: FSMContext):
	if not is_admin(message.from_user.id):
		await state.clear()
		return
	text = (message.text or "").strip()
	data = await state.get_data()
	if _is_cancel(text):
		await state.clear()
		await message.answer("↩️ Отклонение отменено.")
		return
	if len(text) > 300:
		await message.answer("Причина слишком длинная (до 300 символов).")
		return
	reason = None if text in ("-", "") else text
	rid = int(data["request_id"])
	res = await db.reject_request(rid, message.from_user.id, reason)
	await state.clear()
	if not res["ok"]:
		await message.answer(f"❌ {res['message']}")
		return
	await _clear_card(data)
	await message.answer(
		f"❌ <b>Заявка №{rid} отклонена</b>\n"
		f"<blockquote>Резерв снят: <b>{fmt(res['amount'])}</b> голды</blockquote>"
	)
	player_text = f"❌ <b>Заявка №{rid} отклонена</b>\n<blockquote>Голда снова доступна на балансе"
	if reason:
		player_text += f"\n\n💬 Причина: {escape(reason)}"
	player_text += "</blockquote>\n\nНе согласен — оспорь в 📜 Мои заявки."
	await notify_user(res["telegram_id"], player_text)
	await log_action(message.from_user.id, "request_reject", f"№{rid} · {res['amount']} · {reason or 'без причины'}")


@router.message(Forms.dispute_deny_reason)
async def dispute_deny_reason(message: Message, state: FSMContext):
	if not is_admin(message.from_user.id):
		await state.clear()
		return
	text = (message.text or "").strip()
	data = await state.get_data()
	if _is_cancel(text):
		await state.clear()
		await message.answer("↩️ Отклонение спора отменено.")
		return
	if len(text) > 300:
		await message.answer("Ответ слишком длинный (до 300 символов).")
		return
	answer = None if text in ("-", "") else text
	did = int(data["dispute_id"])
	res = await db.deny_dispute(did, message.from_user.id, answer)
	await state.clear()
	if not res["ok"]:
		await message.answer(f"❌ {res['message']}")
		return
	await _clear_card(data)
	await message.answer(f"✖️ <b>Спор №{did} отклонён</b>")
	player_text = f"⚖️ <b>Спор по заявке №{res['request_id']} отклонён</b>"
	if answer:
		player_text += f"\n<blockquote>💬 {escape(answer)}</blockquote>"
	await notify_user(res["telegram_id"], player_text)
	await log_action(message.from_user.id, "dispute_deny", f"спор №{did} · {answer or 'без пояснения'}")


@router.message(Forms.create_promo_code)
async def create_promo(message: Message, state: FSMContext):
	if not is_admin(message.from_user.id):
		await state.clear()
		return
	text = (message.text or "").strip()
	if _is_cancel(text):
		await state.clear()
		await message.answer("↩️ Создание промокода отменено.")
		return
	parts = text.split()
	if len(parts) == 1:
		ok = await do_create_promo(message, None, parts[0])
	elif len(parts) == 2:
		ok = await do_create_promo(message, parts[0], parts[1])
	else:
		await message.answer("Формат: <code>СУММА</code> или <code>КОД СУММА</code>.")
		return
	if ok:
		await state.clear()


@router.message(Forms.find_user)
async def find_user(message: Message, state: FSMContext):
	if not is_admin(message.from_user.id):
		await state.clear()
		return
	text = (message.text or "").strip()
	if _is_cancel(text):
		await state.clear()
		await message.answer("↩️ Поиск отменён.")
		return
	if await send_user_card(message, text):
		await state.clear()


@router.message(Forms.broadcast_text)
async def broadcast_text(message: Message, state: FSMContext):
	if not is_admin(message.from_user.id):
		await state.clear()
		return
	text = (message.text or "").strip()
	if _is_cancel(text):
		await state.clear()
		await message.answer("↩️ Рассылка отменена.")
		return
	if not text or len(text) > 3500:
		await message.answer("Текст от 1 до 3500 символов.")
		return
	# превью: заодно проверяем HTML-разметку
	try:
		await message.answer(text)
	except TelegramBadRequest:
		await message.answer("❌ Ошибка HTML-разметки. Поправь текст и пришли снова.")
		return
	await state.update_data(broadcast_text=text)
	await state.set_state(Forms.broadcast_confirm)
	count = len(await db.get_all_user_ids())
	kb = InlineKeyboardMarkup(inline_keyboard=[[
		InlineKeyboardButton(text="✅ Разослать", callback_data="bc:yes"),
		InlineKeyboardButton(text="🚫 Отмена", callback_data="bc:no"),
	]])
	await message.answer(f"👆 Превью. Получат <b>{count}</b> игроков. Разослать?", reply_markup=kb)
