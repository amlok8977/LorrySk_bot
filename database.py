import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


class Database:
	def __init__(self, path: str):
		self.path = path
		self.conn = None
		# одно соединение на всех: транзакции строго по очереди
		self.lock = asyncio.Lock()

	async def initialize(self):
		self.conn = await aiosqlite.connect(self.path)
		self.conn.row_factory = aiosqlite.Row
		await self.conn.execute("PRAGMA foreign_keys = ON")
		await self.conn.execute("PRAGMA journal_mode = WAL")
		await self.conn.executescript("""
		CREATE TABLE IF NOT EXISTS users (
			telegram_id INTEGER PRIMARY KEY,
			username TEXT NOT NULL DEFAULT '',
			full_name TEXT NOT NULL DEFAULT '',
			balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
			reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
			banned INTEGER NOT NULL DEFAULT 0,
			created_at TEXT NOT NULL
		);
		CREATE TABLE IF NOT EXISTS promo_codes (
			code TEXT PRIMARY KEY,
			amount INTEGER NOT NULL CHECK(amount > 0),
			max_uses INTEGER NOT NULL CHECK(max_uses > 0),
			uses INTEGER NOT NULL DEFAULT 0 CHECK(uses >= 0),
			active INTEGER NOT NULL DEFAULT 1,
			created_by INTEGER NOT NULL,
			created_at TEXT NOT NULL
		);
		CREATE TABLE IF NOT EXISTS promo_activations (
			code TEXT NOT NULL,
			telegram_id INTEGER NOT NULL,
			activated_at TEXT NOT NULL,
			PRIMARY KEY(code, telegram_id),
			FOREIGN KEY(code) REFERENCES promo_codes(code),
			FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
		);
		CREATE TABLE IF NOT EXISTS gold_requests (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			telegram_id INTEGER NOT NULL,
			amount INTEGER NOT NULL CHECK(amount > 0),
			nickname TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
			reject_reason TEXT,
			delivered_at TEXT,
			created_at TEXT NOT NULL,
			decided_at TEXT,
			decided_by INTEGER,
			FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
		);
		CREATE TABLE IF NOT EXISTS disputes (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			request_id INTEGER NOT NULL UNIQUE,
			telegram_id INTEGER NOT NULL,
			reason TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','denied')),
			resolution TEXT,
			admin_answer TEXT,
			created_at TEXT NOT NULL,
			decided_at TEXT,
			decided_by INTEGER,
			FOREIGN KEY(request_id) REFERENCES gold_requests(id),
			FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
		);
		CREATE TABLE IF NOT EXISTS action_log (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			actor_id INTEGER NOT NULL,
			action TEXT NOT NULL,
			details TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL
		);
		""")
		# миграции для старой БД (если файл уже создавался прошлой версией)
		await self._ensure_column("users", "banned", "INTEGER NOT NULL DEFAULT 0")
		await self._ensure_column("promo_codes", "active", "INTEGER NOT NULL DEFAULT 1")
		await self._ensure_column("gold_requests", "reject_reason", "TEXT")
		await self._ensure_column("gold_requests", "delivered_at", "TEXT")
		await self.conn.commit()

	async def _ensure_column(self, table: str, column: str, ddl: str):
		cur = await self.conn.execute(f"PRAGMA table_info({table})")
		cols = [r["name"] for r in await cur.fetchall()]
		if column not in cols:
			await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

	async def close(self):
		if self.conn:
			await self.conn.close()

	async def checkpoint(self):
		# сливаем WAL в основной файл перед бэкапом
		async with self.lock:
			await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

	# ---------- пользователи ----------
	async def register_user(self, telegram_id: int, username: str, full_name: str):
		async with self.lock:
			await self.conn.execute(
				"""INSERT INTO users(telegram_id, username, full_name, created_at)
				VALUES(?,?,?,?)
				ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
				(telegram_id, username, full_name, _now()),
			)
			await self.conn.commit()

	async def _get_user(self, telegram_id: int):
		# без лока: вызывается изнутри методов, которые уже держат лок
		cur = await self.conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
		return await cur.fetchone()

	async def get_user(self, telegram_id: int):
		async with self.lock:
			return await self._get_user(telegram_id)

	async def find_user_id(self, query: str):
		q = query.strip().lstrip("@")
		if not q:
			return None
		async with self.lock:
			if q.isdigit():
				row = await self._get_user(int(q))
			else:
				cur = await self.conn.execute(
					"SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q,)
				)
				row = await cur.fetchone()
			return row["telegram_id"] if row else None

	async def get_user_card(self, telegram_id: int):
		async with self.lock:
			user = await self._get_user(telegram_id)
			if not user:
				return None
			cur = await self.conn.execute(
				"SELECT COUNT(*) FROM promo_activations WHERE telegram_id=?", (telegram_id,)
			)
			acts = (await cur.fetchone())[0]
			cur = await self.conn.execute(
				"SELECT status, COUNT(*) AS n FROM gold_requests WHERE telegram_id=? GROUP BY status",
				(telegram_id,),
			)
			by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
			cur = await self.conn.execute(
				"SELECT COUNT(*) FROM disputes WHERE telegram_id=? AND status='open'", (telegram_id,)
			)
			disputes = (await cur.fetchone())[0]
			return {"user": user, "activations": acts, "requests": by_status, "open_disputes": disputes}

	async def get_all_user_ids(self):
		async with self.lock:
			cur = await self.conn.execute("SELECT telegram_id FROM users WHERE banned=0")
			return [r["telegram_id"] for r in await cur.fetchall()]

	async def get_banned_ids(self):
		async with self.lock:
			cur = await self.conn.execute("SELECT telegram_id FROM users WHERE banned=1")
			return [r["telegram_id"] for r in await cur.fetchall()]

	async def set_banned(self, telegram_id: int, banned: bool) -> bool:
		async with self.lock:
			cur = await self.conn.execute(
				"UPDATE users SET banned=? WHERE telegram_id=?", (1 if banned else 0, telegram_id)
			)
			await self.conn.commit()
			return cur.rowcount == 1

	async def adjust_balance(self, telegram_id: int, delta: int):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				user = await self._get_user(telegram_id)
				if not user:
					await self.conn.rollback()
					return {"ok": False, "message": "Игрок не найден."}
				new_balance = user["balance"] + delta
				if new_balance < user["reserved"]:
					await self.conn.rollback()
					return {"ok": False, "message": f"Баланс не может быть ниже резерва ({user['reserved']})."}
				await self.conn.execute(
					"UPDATE users SET balance=? WHERE telegram_id=?", (new_balance, telegram_id)
				)
				await self.conn.commit()
				return {"ok": True, "balance": new_balance}
			except Exception:
				await self.conn.rollback()
				raise

	# ---------- промокоды ----------
	async def create_promo(self, code: str, amount: int, max_uses: int, created_by: int) -> bool:
		async with self.lock:
			try:
				await self.conn.execute(
					"INSERT INTO promo_codes(code,amount,max_uses,created_by,created_at) VALUES(?,?,?,?,?)",
					(code, amount, max_uses, created_by, _now()),
				)
				await self.conn.commit()
				return True
			except aiosqlite.IntegrityError:
				await self.conn.rollback()
				return False
			except Exception:
				await self.conn.rollback()
				raise

	async def list_promos(self, limit: int = 15):
		async with self.lock:
			cur = await self.conn.execute(
				"SELECT rowid AS rid, * FROM promo_codes ORDER BY rowid DESC LIMIT ?", (limit,)
			)
			return await cur.fetchall()

	async def toggle_promo(self, rid: int):
		async with self.lock:
			cur = await self.conn.execute(
				"UPDATE promo_codes SET active = 1 - active WHERE rowid=?", (rid,)
			)
			await self.conn.commit()
			if cur.rowcount != 1:
				return None
			cur = await self.conn.execute("SELECT rowid AS rid, * FROM promo_codes WHERE rowid=?", (rid,))
			return await cur.fetchone()

	async def redeem_promo(self, telegram_id: int, code: str):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				user = await self._get_user(telegram_id)
				if not user:
					await self.conn.rollback()
					return {"ok": False, "code": "nouser", "message": "Сначала нажми /start."}
				cur = await self.conn.execute("SELECT * FROM promo_codes WHERE code=?", (code,))
				promo = await cur.fetchone()
				if not promo:
					await self.conn.rollback()
					return {"ok": False, "code": "notfound", "message": "❌ Промокод не найден."}
				if not promo["active"]:
					await self.conn.rollback()
					return {"ok": False, "code": "inactive", "message": "❌ Промокод не найден."}
				cur = await self.conn.execute(
					"SELECT 1 FROM promo_activations WHERE code=? AND telegram_id=?", (code, telegram_id)
				)
				if await cur.fetchone():
					await self.conn.rollback()
					return {"ok": False, "code": "used", "message": "❌ Ты уже активировал этот промокод."}
				if promo["uses"] >= promo["max_uses"]:
					await self.conn.rollback()
					return {"ok": False, "code": "limit", "message": "❌ Лимит активаций этого промокода исчерпан."}
				await self.conn.execute(
					"INSERT INTO promo_activations(code,telegram_id,activated_at) VALUES(?,?,?)",
					(code, telegram_id, _now()),
				)
				await self.conn.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
				await self.conn.execute(
					"UPDATE users SET balance=balance+? WHERE telegram_id=?", (promo["amount"], telegram_id)
				)
				await self.conn.commit()
				return {"ok": True, "amount": promo["amount"]}
			except Exception:
				await self.conn.rollback()
				raise

	async def list_user_activations(self, telegram_id: int, limit: int = 10):
		async with self.lock:
			cur = await self.conn.execute(
				"""SELECT a.code, a.activated_at, p.amount FROM promo_activations a
				JOIN promo_codes p ON p.code=a.code
				WHERE a.telegram_id=? ORDER BY a.activated_at DESC LIMIT ?""",
				(telegram_id, limit),
			)
			return await cur.fetchall()

	# ---------- заявки ----------
	async def get_pending_request(self, telegram_id: int):
		async with self.lock:
			cur = await self.conn.execute(
				"SELECT * FROM gold_requests WHERE telegram_id=? AND status='pending' LIMIT 1", (telegram_id,)
			)
			return await cur.fetchone()

	async def create_request(self, telegram_id: int, amount: int, nickname: str):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				user = await self._get_user(telegram_id)
				if not user:
					await self.conn.rollback()
					return {"ok": False, "message": "Сначала нажми /start."}
				cur = await self.conn.execute(
					"SELECT id FROM gold_requests WHERE telegram_id=? AND status='pending' LIMIT 1", (telegram_id,)
				)
				active = await cur.fetchone()
				if active:
					await self.conn.rollback()
					return {
						"ok": False,
						"message": f"❌ У тебя уже есть активная заявка №{active['id']}. "
						"Дождись решения или отмени её в 📜 Мои заявки.",
					}
				if amount > user["balance"] - user["reserved"]:
					await self.conn.rollback()
					return {"ok": False, "message": "❌ Недостаточно доступной голды."}
				await self.conn.execute(
					"UPDATE users SET reserved=reserved+? WHERE telegram_id=?", (amount, telegram_id)
				)
				cur = await self.conn.execute(
					"INSERT INTO gold_requests(telegram_id,amount,nickname,status,created_at) VALUES(?,?,?,'pending',?)",
					(telegram_id, amount, nickname, _now()),
				)
				request_id = cur.lastrowid
				await self.conn.commit()
				return {"ok": True, "request_id": request_id, "amount": amount}
			except Exception:
				await self.conn.rollback()
				raise

	async def get_request(self, request_id: int):
		async with self.lock:
			cur = await self.conn.execute(
				"""SELECT r.*, u.full_name, u.username FROM gold_requests r
				JOIN users u ON u.telegram_id=r.telegram_id WHERE r.id=?""",
				(request_id,),
			)
			return await cur.fetchone()

	async def list_pending_requests(self):
		async with self.lock:
			cur = await self.conn.execute(
				"""SELECT r.*, u.full_name, u.username FROM gold_requests r
				JOIN users u ON u.telegram_id=r.telegram_id
				WHERE r.status='pending' ORDER BY r.id DESC"""
			)
			return await cur.fetchall()

	async def list_undelivered_requests(self):
		async with self.lock:
			cur = await self.conn.execute(
				"""SELECT r.*, u.full_name, u.username FROM gold_requests r
				JOIN users u ON u.telegram_id=r.telegram_id
				WHERE r.status='approved' AND r.delivered_at IS NULL ORDER BY r.id DESC"""
			)
			return await cur.fetchall()

	async def list_user_requests(self, telegram_id: int, limit: int = 10):
		async with self.lock:
			cur = await self.conn.execute(
				"""SELECT r.*, d.status AS dispute_status FROM gold_requests r
				LEFT JOIN disputes d ON d.request_id=r.id
				WHERE r.telegram_id=? ORDER BY r.id DESC LIMIT ?""",
				(telegram_id, limit),
			)
			return await cur.fetchall()

	async def _decide_request(self, request_id: int, admin_id: int, approve: bool, reason=None):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				cur = await self.conn.execute("SELECT * FROM gold_requests WHERE id=?", (request_id,))
				req = await cur.fetchone()
				if not req or req["status"] != "pending":
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка уже обработана или не найдена."}
				user = await self._get_user(req["telegram_id"])
				if not user or user["reserved"] < req["amount"] or (approve and user["balance"] < req["amount"]):
					await self.conn.rollback()
					return {"ok": False, "message": "Ошибка баланса/резерва. Нужна проверка базы."}
				if approve:
					await self.conn.execute(
						"UPDATE users SET balance=balance-?, reserved=reserved-? WHERE telegram_id=?",
						(req["amount"], req["amount"], req["telegram_id"]),
					)
				else:
					await self.conn.execute(
						"UPDATE users SET reserved=reserved-? WHERE telegram_id=?",
						(req["amount"], req["telegram_id"]),
					)
				cur = await self.conn.execute(
					"""UPDATE gold_requests SET status=?, reject_reason=?, decided_at=?, decided_by=?
					WHERE id=? AND status='pending'""",
					("approved" if approve else "rejected", None if approve else reason, _now(), admin_id, request_id),
				)
				if cur.rowcount != 1:
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка уже обработана."}
				await self.conn.commit()
				return {
					"ok": True,
					"telegram_id": req["telegram_id"],
					"amount": req["amount"],
					"nickname": req["nickname"],
				}
			except Exception:
				await self.conn.rollback()
				raise

	async def approve_request(self, request_id: int, admin_id: int):
		return await self._decide_request(request_id, admin_id, True)

	async def reject_request(self, request_id: int, admin_id: int, reason=None):
		return await self._decide_request(request_id, admin_id, False, reason)

	async def mark_delivered(self, request_id: int, admin_id: int):
		async with self.lock:
			cur = await self.conn.execute(
				"""UPDATE gold_requests SET delivered_at=?
				WHERE id=? AND status='approved' AND delivered_at IS NULL""",
				(_now(), request_id),
			)
			await self.conn.commit()
			if cur.rowcount != 1:
				return {"ok": False, "message": "Заявка уже отмечена выданной или не одобрена."}
			cur = await self.conn.execute("SELECT * FROM gold_requests WHERE id=?", (request_id,))
			req = await cur.fetchone()
			return {
				"ok": True,
				"telegram_id": req["telegram_id"],
				"amount": req["amount"],
				"nickname": req["nickname"],
			}

	async def cancel_request(self, request_id: int, telegram_id: int):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				cur = await self.conn.execute("SELECT * FROM gold_requests WHERE id=?", (request_id,))
				req = await cur.fetchone()
				if not req or req["telegram_id"] != telegram_id:
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка не найдена."}
				if req["status"] != "pending":
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка уже обработана, отменить нельзя."}
				user = await self._get_user(telegram_id)
				if not user or user["reserved"] < req["amount"]:
					await self.conn.rollback()
					return {"ok": False, "message": "Ошибка резерва. Напиши админу."}
				await self.conn.execute(
					"UPDATE users SET reserved=reserved-? WHERE telegram_id=?", (req["amount"], telegram_id)
				)
				cur = await self.conn.execute(
					"""UPDATE gold_requests SET status='rejected', reject_reason='Отменено игроком',
					decided_at=?, decided_by=? WHERE id=? AND status='pending'""",
					(_now(), telegram_id, request_id),
				)
				if cur.rowcount != 1:
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка уже обработана."}
				await self.conn.commit()
				return {"ok": True, "amount": req["amount"]}
			except Exception:
				await self.conn.rollback()
				raise

	# ---------- споры ----------
	async def _dispute_block(self, req, telegram_id: int):
		# без лока. Возвращает текст причины отказа или None, если оспорить можно
		if not req or req["telegram_id"] != telegram_id:
			return "Заявка не найдена."
		if req["status"] == "pending":
			return "Заявка ещё не решена. Её можно отменить."
		if req["status"] == "rejected" and req["decided_by"] == telegram_id:
			return "Свою отмену оспорить нельзя."
		cur = await self.conn.execute("SELECT 1 FROM disputes WHERE request_id=?", (req["id"],))
		if await cur.fetchone():
			return "Спор по этой заявке уже есть."
		return None

	async def can_dispute(self, telegram_id: int, request_id: int):
		async with self.lock:
			cur = await self.conn.execute("SELECT * FROM gold_requests WHERE id=?", (request_id,))
			req = await cur.fetchone()
			block = await self._dispute_block(req, telegram_id)
			return (block is None), block

	async def create_dispute(self, telegram_id: int, request_id: int, reason: str):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				cur = await self.conn.execute("SELECT * FROM gold_requests WHERE id=?", (request_id,))
				req = await cur.fetchone()
				block = await self._dispute_block(req, telegram_id)
				if block:
					await self.conn.rollback()
					return {"ok": False, "message": block}
				cur = await self.conn.execute(
					"INSERT INTO disputes(request_id,telegram_id,reason,status,created_at) VALUES(?,?,?,'open',?)",
					(request_id, telegram_id, reason, _now()),
				)
				dispute_id = cur.lastrowid
				await self.conn.commit()
				return {"ok": True, "dispute_id": dispute_id}
			except Exception:
				await self.conn.rollback()
				raise

	_DISPUTE_SELECT = """SELECT d.*, r.amount, r.nickname, r.status AS req_status, r.delivered_at,
		r.reject_reason, u.full_name, u.username
		FROM disputes d
		JOIN gold_requests r ON r.id=d.request_id
		JOIN users u ON u.telegram_id=d.telegram_id"""

	async def get_dispute(self, dispute_id: int):
		async with self.lock:
			cur = await self.conn.execute(self._DISPUTE_SELECT + " WHERE d.id=?", (dispute_id,))
			return await cur.fetchone()

	async def list_open_disputes(self):
		async with self.lock:
			cur = await self.conn.execute(
				self._DISPUTE_SELECT + " WHERE d.status='open' ORDER BY d.id DESC"
			)
			return await cur.fetchall()

	async def accept_dispute(self, dispute_id: int, admin_id: int):
		async with self.lock:
			await self.conn.execute("BEGIN IMMEDIATE")
			try:
				cur = await self.conn.execute("SELECT * FROM disputes WHERE id=?", (dispute_id,))
				disp = await cur.fetchone()
				if not disp or disp["status"] != "open":
					await self.conn.rollback()
					return {"ok": False, "message": "Спор уже обработан или не найден."}
				cur = await self.conn.execute("SELECT * FROM gold_requests WHERE id=?", (disp["request_id"],))
				req = await cur.fetchone()
				user = await self._get_user(disp["telegram_id"])
				if not req or not user:
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка или игрок не найдены."}
				if req["status"] == "approved":
					# голда была списана, а игрок её не получил -> возвращаем на баланс
					mode = "refund"
					await self.conn.execute(
						"UPDATE users SET balance=balance+? WHERE telegram_id=?",
						(req["amount"], req["telegram_id"]),
					)
				elif req["status"] == "rejected":
					# отказ был зря -> заявка снова в работе, сумма снова в резерве
					mode = "reopen"
					cur = await self.conn.execute(
						"SELECT id FROM gold_requests WHERE telegram_id=? AND status='pending' LIMIT 1",
						(req["telegram_id"],),
					)
					if await cur.fetchone():
						await self.conn.rollback()
						return {"ok": False, "message": "У игрока уже есть активная заявка. Пусть дождётся решения."}
					if req["amount"] > user["balance"] - user["reserved"]:
						await self.conn.rollback()
						return {"ok": False, "message": "У игрока сейчас не хватает доступной голды для повторного резерва."}
					await self.conn.execute(
						"UPDATE users SET reserved=reserved+? WHERE telegram_id=?",
						(req["amount"], req["telegram_id"]),
					)
					await self.conn.execute(
						"""UPDATE gold_requests SET status='pending', reject_reason=NULL,
						decided_at=NULL, decided_by=NULL WHERE id=?""",
						(req["id"],),
					)
				else:
					await self.conn.rollback()
					return {"ok": False, "message": "Заявка в ожидании, спор не нужен."}
				await self.conn.execute(
					"UPDATE disputes SET status='accepted', resolution=?, decided_at=?, decided_by=? WHERE id=?",
					(mode, _now(), admin_id, dispute_id),
				)
				await self.conn.commit()
				return {
					"ok": True,
					"mode": mode,
					"telegram_id": req["telegram_id"],
					"amount": req["amount"],
					"request_id": req["id"],
				}
			except Exception:
				await self.conn.rollback()
				raise

	async def deny_dispute(self, dispute_id: int, admin_id: int, answer=None):
		async with self.lock:
			cur = await self.conn.execute(
				"""UPDATE disputes SET status='denied', admin_answer=?, decided_at=?, decided_by=?
				WHERE id=? AND status='open'""",
				(answer, _now(), admin_id, dispute_id),
			)
			await self.conn.commit()
			if cur.rowcount != 1:
				return {"ok": False, "message": "Спор уже обработан или не найден."}
			cur = await self.conn.execute(
				"SELECT telegram_id, request_id FROM disputes WHERE id=?", (dispute_id,)
			)
			row = await cur.fetchone()
			return {"ok": True, "telegram_id": row["telegram_id"], "request_id": row["request_id"]}

	# ---------- лог и мониторинг ----------
	async def add_log(self, actor_id: int, action: str, details: str = ""):
		async with self.lock:
			await self.conn.execute(
				"INSERT INTO action_log(actor_id,action,details,created_at) VALUES(?,?,?,?)",
				(actor_id, action, details, _now()),
			)
			await self.conn.commit()

	async def list_logs(self, limit: int = 15):
		async with self.lock:
			cur = await self.conn.execute(
				"SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
			)
			return await cur.fetchall()

	async def get_stats(self):
		now = datetime.now(timezone.utc)
		today = now.date().isoformat()
		week = (now - timedelta(days=7)).date().isoformat()
		async with self.lock:
			cur = await self.conn.execute("""SELECT
				(SELECT COUNT(*) FROM users) AS users,
				(SELECT COUNT(*) FROM promo_codes) AS promos,
				(SELECT COALESCE(SUM(uses),0) FROM promo_codes) AS uses,
				(SELECT COALESCE(SUM(amount*uses),0) FROM promo_codes) AS promo_paid,
				(SELECT COALESCE(SUM(reserved),0) FROM users) AS reserved,
				(SELECT COALESCE(SUM(amount),0) FROM gold_requests WHERE status='approved') AS approved_sum,
				(SELECT COUNT(*) FROM gold_requests WHERE status='pending') AS pending,
				(SELECT COUNT(*) FROM gold_requests WHERE status='approved' AND delivered_at IS NULL) AS undelivered,
				(SELECT COUNT(*) FROM disputes WHERE status='open') AS open_disputes,
				(SELECT COUNT(*) FROM users WHERE created_at >= ?) AS today_users,
				(SELECT COUNT(*) FROM promo_activations WHERE activated_at >= ?) AS today_uses,
				(SELECT COUNT(*) FROM gold_requests WHERE created_at >= ?) AS today_requests,
				(SELECT COUNT(*) FROM users WHERE created_at >= ?) AS week_users,
				(SELECT COUNT(*) FROM promo_activations WHERE activated_at >= ?) AS week_uses,
				(SELECT COUNT(*) FROM gold_requests WHERE created_at >= ?) AS week_requests""",
				(today, today, today, week, week, week),
			)
			return dict(await cur.fetchone())
